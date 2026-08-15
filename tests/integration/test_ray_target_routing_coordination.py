"""Focused database contracts for dormant Ray target-route coordination."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from inspect import signature
from threading import Barrier, Lock, get_ident
from typing import Any

import pytest
from django.db import (
    IntegrityError,
    OperationalError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.db.models import F
from django.db.models.query import QuerySet
from django.test.utils import CaptureQueriesContext

import django_ray.target_routing as routing
from django_ray.models import (
    RayTarget,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayTargetRoute,
    RayTargetRouteRevision,
    RayTaskTargetRouteSelection,
)
from django_ray.target_attestation import (
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    decode_ray_target_expectation,
)
from django_ray.target_coordination import (
    register_ray_target,
    transition_ray_target_desired_state,
)
from django_ray.target_routing import (
    InvalidRayTargetRouteArgumentError,
    NestedRayTargetRouteTransactionError,
    RayJobTargetRoutingUnsupportedError,
    RayTargetRouteChange,
    RayTargetRouteNotFoundError,
    RayTargetRoutePersistenceRaceError,
    RayTargetRoutePolicyRevisionConflictError,
    RayTargetRoutePolicyStateError,
    RayTargetRouteRegistrationConflictError,
    RayTargetRouteRevisionConflictError,
    RayTargetRouteRevisionExhaustedError,
    RayTargetRouteStateError,
    RayTargetRouteTargetNotActiveError,
    UnsupportedRayTargetRouteDatabaseError,
    register_ray_target_route,
    transition_ray_target_route,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _runtime() -> RayRuntimeVersion:
    return RayRuntimeVersion(
        ray_major=2,
        ray_minor=56,
        ray_patch=0,
        python_implementation="cpython",
        python_major=3,
        python_minor=12,
        python_patch=12,
    )


def _expectation(
    *,
    target_key: str,
    session_suffix: str,
    runner_family: RayRunnerFamily = RayRunnerFamily.RAY_CORE,
) -> RayTargetExpectation:
    return RayTargetExpectation(
        target_key=target_key,
        runner_family=runner_family,
        cluster_session=f"session_2026-08-15_12-00-00_123456_{session_suffix}",
        policy_revision=1,
        runtime=_runtime(),
    )


def _target(
    target_key: str = "target.primary",
    *,
    session_suffix: str = "1",
    active: bool = True,
) -> RayTargetPolicyRevision:
    register_ray_target(_expectation(target_key=target_key, session_suffix=session_suffix))
    revision = 1
    if active:
        change = transition_ray_target_desired_state(
            target_key,
            RayTargetDesiredState.ACTIVE,
            expected_policy_revision=1,
        )
        revision = change.revision
    return RayTargetPolicyRevision.objects.get(target_id=target_key, revision=revision)


def _register_route(
    *,
    backend_alias: str = "ray-default",
    target_key: str = "target.primary",
    target_policy_revision: int = 2,
) -> RayTargetRouteChange:
    return register_ray_target_route(
        backend_alias,
        target_key,
        expected_target_policy_revision=target_policy_revision,
    )


def _require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


def _run_concurrently(*operations):
    barrier = Barrier(len(operations))

    def invoke(operation):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return operation()
            except Exception as error:  # preserve the exact typed race outcome
                return error
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(invoke, operation) for operation in operations]
        return [future.result(timeout=20) for future in futures]


def test_registration_persists_exact_revision_one_without_task_selection() -> None:
    policy = _target()

    result = _register_route()

    assert result == RayTargetRouteChange(
        backend_alias="ray-default",
        target_key="target.primary",
        target_policy_revision=2,
        changed=True,
        previous_revision=0,
        revision=1,
        expectation=decode_ray_target_expectation(policy.expectation_json),
    )
    assert RayTargetRoute.objects.get(pk="ray-default").backend_alias == "ray-default"
    revision = RayTargetRouteRevision.objects.get(route_id="ray-default", revision=1)
    assert revision.target_policy_id == policy.pk
    assert not RayTaskTargetRouteSelection.objects.exists()


def test_registration_is_idempotent_only_for_the_same_valid_revision_one() -> None:
    policy = _target()
    first = _register_route()

    repeated = _register_route()

    assert first.changed is True
    assert repeated == RayTargetRouteChange(
        backend_alias="ray-default",
        target_key="target.primary",
        target_policy_revision=2,
        changed=False,
        previous_revision=1,
        revision=1,
        expectation=decode_ray_target_expectation(policy.expectation_json),
    )
    assert RayTargetRouteRevision.objects.count() == 1


def test_registration_conflicts_with_a_different_valid_policy() -> None:
    _target()
    _target("target.secondary", session_suffix="2")
    _register_route()

    with pytest.raises(RayTargetRouteRegistrationConflictError) as error:
        _register_route(target_key="target.secondary")

    assert str(error.value) == "Ray target route registration conflicts with durable state"
    assert RayTargetRouteRevision.objects.count() == 1


def test_registration_does_not_reinterpret_a_transitioned_route_as_revision_one() -> None:
    _target()
    _target("target.secondary", session_suffix="2")
    _register_route()
    transition_ray_target_route(
        "ray-default",
        "target.secondary",
        expected_route_revision=1,
        expected_target_policy_revision=2,
    )

    with pytest.raises(RayTargetRouteRegistrationConflictError):
        _register_route(target_key="target.secondary")


def test_transition_appends_immutable_history_for_an_active_candidate() -> None:
    primary = _target()
    secondary = _target("target.secondary", session_suffix="2")
    _register_route()

    result = transition_ray_target_route(
        "ray-default",
        "target.secondary",
        expected_route_revision=1,
        expected_target_policy_revision=2,
    )

    assert result == RayTargetRouteChange(
        backend_alias="ray-default",
        target_key="target.secondary",
        target_policy_revision=2,
        changed=True,
        previous_revision=1,
        revision=2,
        expectation=decode_ray_target_expectation(secondary.expectation_json),
    )
    assert list(
        RayTargetRouteRevision.objects.order_by("revision").values_list(
            "revision", "target_policy_id"
        )
    ) == [(1, primary.pk), (2, secondary.pk)]


def test_transition_to_the_exact_same_policy_is_an_idempotent_noop() -> None:
    policy = _target()
    _register_route()

    result = transition_ray_target_route(
        "ray-default",
        "target.primary",
        expected_route_revision=1,
        expected_target_policy_revision=2,
    )

    assert result == RayTargetRouteChange(
        backend_alias="ray-default",
        target_key="target.primary",
        target_policy_revision=2,
        changed=False,
        previous_revision=1,
        revision=1,
        expectation=decode_ray_target_expectation(policy.expectation_json),
    )
    assert RayTargetRouteRevision.objects.count() == 1


def test_transition_rejects_stale_revisions_and_missing_routes() -> None:
    _target()
    _register_route()

    with pytest.raises(RayTargetRouteRevisionConflictError) as stale:
        transition_ray_target_route(
            "ray-default",
            "target.primary",
            expected_route_revision=2,
            expected_target_policy_revision=2,
        )
    assert (stale.value.expected_revision, stale.value.actual_revision) == (2, 1)

    with pytest.raises(RayTargetRouteNotFoundError) as absent:
        transition_ray_target_route(
            "missing-route",
            "target.primary",
            expected_route_revision=1,
            expected_target_policy_revision=2,
        )
    assert str(absent.value) == "Ray target routing could not find the route"
    assert "missing-route" not in str(absent.value)


def test_transition_rejects_a_stale_candidate_policy_revision() -> None:
    _target()
    _register_route()

    with pytest.raises(RayTargetRoutePolicyRevisionConflictError) as stale:
        transition_ray_target_route(
            "ray-default",
            "target.primary",
            expected_route_revision=1,
            expected_target_policy_revision=1,
        )

    assert (stale.value.expected_revision, stale.value.actual_revision) == (1, 2)
    assert str(stale.value) == "Ray target route policy revision changed"


def test_candidate_policy_must_be_latest_active_and_ray_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _target("target.draining", session_suffix="2", active=False)
    with pytest.raises(RayTargetRouteTargetNotActiveError) as inactive:
        register_ray_target_route(
            "draining-route",
            "target.draining",
            expected_target_policy_revision=1,
        )
    assert not RayTargetRoute.objects.filter(pk="draining-route").exists()
    assert str(inactive.value) == "Ray target route candidate is not active"

    active = _target()
    retained_latest = routing._latest_policy

    def ray_job_latest(target: RayTarget, *, using: str):
        policy, expectation, state = retained_latest(target, using=using)
        return policy, replace(expectation, runner_family=RayRunnerFamily.RAY_JOB), state

    monkeypatch.setattr(routing, "_latest_policy", ray_job_latest)
    with pytest.raises(RayJobTargetRoutingUnsupportedError) as unsupported:
        register_ray_target_route(
            "ray-job-route",
            active.target_id,
            expected_target_policy_revision=2,
        )
    assert not RayTargetRoute.objects.filter(pk="ray-job-route").exists()
    assert str(unsupported.value) == "Ray Job target routing is not supported"


def test_missing_or_corrupt_candidate_policy_is_a_fixed_refusal() -> None:
    with pytest.raises(RayTargetRoutePolicyStateError) as missing:
        register_ray_target_route(
            "private-route",
            "private-target",
            expected_target_policy_revision=1,
        )
    assert str(missing.value) == "Ray target route policy is unavailable or invalid"
    assert "private" not in str(missing.value)

    _target()
    RayTargetPolicyRevision.objects.get(target_id="target.primary", revision=1).delete()
    with pytest.raises(RayTargetRoutePolicyStateError):
        register_ray_target_route(
            "corrupt-route",
            "target.primary",
            expected_target_policy_revision=2,
        )


def test_inactive_new_policy_invalidates_an_old_route_noop() -> None:
    _target()
    _register_route()
    transition_ray_target_desired_state(
        "target.primary",
        RayTargetDesiredState.DRAINING,
        expected_policy_revision=2,
    )

    with pytest.raises(RayTargetRouteTargetNotActiveError):
        transition_ray_target_route(
            "ray-default",
            "target.primary",
            expected_route_revision=1,
            expected_target_policy_revision=3,
        )


def test_empty_and_gapped_route_history_fail_closed() -> None:
    _target()
    RayTargetRoute.objects.create(backend_alias="empty-route")
    with pytest.raises(RayTargetRouteStateError):
        _register_route(backend_alias="empty-route")

    _target("target.secondary", session_suffix="2")
    _register_route()
    transition_ray_target_route(
        "ray-default",
        "target.secondary",
        expected_route_revision=1,
        expected_target_policy_revision=2,
    )
    RayTargetRouteRevision.objects.get(route_id="ray-default", revision=1).delete()
    with pytest.raises(RayTargetRouteStateError):
        transition_ray_target_route(
            "ray-default",
            "target.primary",
            expected_route_revision=2,
            expected_target_policy_revision=2,
        )


def test_route_history_starting_at_revision_two_fails_closed() -> None:
    policy = _target()
    route = RayTargetRoute.objects.create(backend_alias="corrupt-route")
    RayTargetRouteRevision.objects.create(
        route=route,
        revision=2,
        target_policy=policy,
    )

    with pytest.raises(RayTargetRouteStateError):
        transition_ray_target_route(
            "corrupt-route",
            "target.primary",
            expected_route_revision=2,
            expected_target_policy_revision=2,
        )


def test_changing_transition_refuses_revision_exhaustion_but_noop_remains_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _target()
    _target("target.secondary", session_suffix="2")
    _register_route()
    transition_ray_target_route(
        "ray-default",
        "target.secondary",
        expected_route_revision=1,
        expected_target_policy_revision=2,
    )
    monkeypatch.setattr(routing, "_MAX_REVISION", 2)

    unchanged = transition_ray_target_route(
        "ray-default",
        "target.secondary",
        expected_route_revision=2,
        expected_target_policy_revision=2,
    )
    assert unchanged.changed is False
    with pytest.raises(RayTargetRouteRevisionExhaustedError) as exhausted:
        transition_ray_target_route(
            "ray-default",
            "target.primary",
            expected_route_revision=2,
            expected_target_policy_revision=2,
        )
    assert str(exhausted.value) == "Ray target route revision is exhausted"


@pytest.mark.parametrize(
    ("operation", "private_value"),
    [
        (
            lambda: register_ray_target_route(
                "Private Route",
                "target.primary",
                expected_target_policy_revision=2,
            ),
            "Private Route",
        ),
        (
            lambda: register_ray_target_route(
                "private-route",
                "Private Target",
                expected_target_policy_revision=2,
            ),
            "Private Target",
        ),
        (
            lambda: register_ray_target_route(
                "private-route",
                "target.primary",
                expected_target_policy_revision=True,
            ),
            "True",
        ),
        (
            lambda: transition_ray_target_route(
                "private-route",
                "target.primary",
                expected_route_revision=0,
                expected_target_policy_revision=2,
            ),
            "private-route",
        ),
        (
            lambda: transition_ray_target_route(
                "private-route",
                "target.primary",
                expected_route_revision=1,
                expected_target_policy_revision=0,
            ),
            "private-route",
        ),
    ],
)
def test_public_arguments_are_exact_bounded_and_errors_do_not_echo(
    operation,
    private_value: str,
) -> None:
    with pytest.raises(InvalidRayTargetRouteArgumentError) as error:
        operation()

    assert str(error.value) == "Ray target routing received an invalid argument"
    assert private_value not in str(error.value)


@pytest.mark.parametrize("using", ["", "missing-private-database"])
def test_invalid_database_aliases_are_fixed_refusals(using: str) -> None:
    with pytest.raises(UnsupportedRayTargetRouteDatabaseError) as error:
        register_ray_target_route(
            "ray-default",
            "target.primary",
            expected_target_policy_revision=2,
            using=using,
        )

    assert str(error.value) == "Ray target routing supports only SQLite and PostgreSQL"
    if using:
        assert using not in str(error.value)


def test_unsupported_database_vendor_is_a_fixed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connections["default"], "vendor", "mysql")

    with pytest.raises(UnsupportedRayTargetRouteDatabaseError):
        register_ray_target_route(
            "ray-default",
            "target.primary",
            expected_target_policy_revision=2,
        )


def test_mutations_require_the_outermost_transaction() -> None:
    _target()

    with transaction.atomic():
        with pytest.raises(NestedRayTargetRouteTransactionError) as registration:
            _register_route()
        with pytest.raises(NestedRayTargetRouteTransactionError):
            transition_ray_target_route(
                "ray-default",
                "target.primary",
                expected_route_revision=1,
                expected_target_policy_revision=2,
            )

    assert str(registration.value) == (
        "Ray target routing must own the outermost database transaction"
    )
    assert not RayTargetRoute.objects.exists()


@pytest.mark.skipif(connection.vendor != "sqlite", reason="requires SQLite query semantics")
def test_sqlite_writer_fence_and_lock_order_are_route_then_target() -> None:
    _target()
    _register_route()

    with CaptureQueriesContext(connection) as queries:
        _register_route()

    statements = [query["sql"] for query in queries.captured_queries]
    begin_index = next(index for index, sql in enumerate(statements) if sql == "BEGIN")
    route_update = next(
        index
        for index, sql in enumerate(statements)
        if sql.startswith('UPDATE "django_ray_raytargetroute"')
    )
    target_update = next(
        index
        for index, sql in enumerate(statements)
        if sql.startswith('UPDATE "django_ray_raytarget"')
    )
    assert route_update == begin_index + 1
    assert route_update < target_update
    assert (
        '"backend_alias" = "django_ray_raytargetroute"."backend_alias"' in statements[route_update]
    )


@pytest.mark.skipif(connection.vendor != "sqlite", reason="requires SQLite trigger semantics")
def test_sqlite_route_guard_permits_only_the_exact_writer_fence_noop() -> None:
    _target()
    _register_route()

    updated = RayTargetRoute.objects.filter(pk="ray-default").update(
        backend_alias=F("backend_alias")
    )

    assert updated == 1
    assert RayTargetRoute.objects.get(pk="ray-default").backend_alias == "ray-default"


def test_postgresql_code_path_locks_route_before_candidate_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _target()
    _register_route()
    locked_models: list[type[Any]] = []
    original_select_for_update = QuerySet.select_for_update

    def tracked_select_for_update(self, *args: Any, **kwargs: Any):
        locked_models.append(self.model)
        return original_select_for_update(self, *args, **kwargs)

    monkeypatch.setattr(routing, "_database_vendor", lambda *, using: "postgresql")
    monkeypatch.setattr(routing, "_postgresql_route_namespace_lock", lambda **kwargs: None)
    monkeypatch.setattr(QuerySet, "select_for_update", tracked_select_for_update)

    _register_route()

    assert locked_models[:2] == [RayTargetRoute, RayTarget]


@pytest.mark.postgresql
def test_postgresql_advisory_lock_serializes_absent_route_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    _target()
    observed_lock_order: dict[int, list[str]] = {}
    observation_lock = Lock()
    original_route_lock = routing._postgresql_route_namespace_lock
    original_target_lock = routing._locked_candidate_target

    def tracked_route_lock(*, backend_alias: str, using: str) -> None:
        original_route_lock(backend_alias=backend_alias, using=using)
        with observation_lock:
            observed_lock_order.setdefault(get_ident(), []).append("route")

    def tracked_target_lock(*, target_key: str, using: str, vendor: str):
        with observation_lock:
            observed_lock_order.setdefault(get_ident(), []).append("target")
        return original_target_lock(target_key=target_key, using=using, vendor=vendor)

    monkeypatch.setattr(routing, "_postgresql_route_namespace_lock", tracked_route_lock)
    monkeypatch.setattr(routing, "_locked_candidate_target", tracked_target_lock)

    results = _run_concurrently(_register_route, _register_route)

    assert all(isinstance(result, RayTargetRouteChange) for result in results)
    assert sorted(result.changed for result in results) == [False, True]
    assert all(
        (result.previous_revision, result.revision) in {(0, 1), (1, 1)} for result in results
    )
    assert len(observed_lock_order) == 2
    assert all(order == ["route", "target"] for order in observed_lock_order.values())
    assert list(RayTargetRouteRevision.objects.values_list("route_id", "revision")) == [
        ("ray-default", 1)
    ]


@pytest.mark.postgresql
def test_postgresql_route_lock_serializes_competing_expected_revision_switches() -> None:
    _require_postgresql()
    primary = _target()
    secondary = _target("target.secondary", session_suffix="2")
    tertiary = _target("target.tertiary", session_suffix="3")
    _register_route()

    def switch(target_key: str) -> RayTargetRouteChange:
        return transition_ray_target_route(
            "ray-default",
            target_key,
            expected_route_revision=1,
            expected_target_policy_revision=2,
        )

    results = _run_concurrently(
        lambda: switch("target.secondary"),
        lambda: switch("target.tertiary"),
    )

    changes = [result for result in results if isinstance(result, RayTargetRouteChange)]
    conflicts = [
        result for result in results if isinstance(result, RayTargetRouteRevisionConflictError)
    ]
    assert len(changes) == 1
    assert (changes[0].previous_revision, changes[0].revision) == (1, 2)
    assert len(conflicts) == 1
    assert (conflicts[0].expected_revision, conflicts[0].actual_revision) == (1, 2)
    assert list(
        RayTargetRouteRevision.objects.order_by("revision").values_list(
            "revision", "target_policy_id"
        )
    ) in [
        [(1, primary.pk), (2, secondary.pk)],
        [(1, primary.pk), (2, tertiary.pk)],
    ]


def test_route_update_anomalies_are_fixed_state_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _target()
    original_update = QuerySet.update

    def report_multiple(self, **kwargs: Any) -> int:
        if self.model is RayTargetRoute:
            return 2
        return original_update(self, **kwargs)

    monkeypatch.setattr(QuerySet, "update", report_multiple)
    with pytest.raises(RayTargetRouteStateError):
        _register_route()


def test_route_disappearing_after_the_writer_fence_is_a_fixed_state_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _target()
    _register_route()
    original_get = QuerySet.get

    def lose_route(self, *args: Any, **kwargs: Any):
        if self.model is RayTargetRoute:
            raise RayTargetRoute.DoesNotExist
        return original_get(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "get", lose_route)
    with pytest.raises(RayTargetRouteStateError):
        _register_route()


def test_database_failures_are_mapped_without_private_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _target()
    poison = "private-database-route-secret"
    original_create = QuerySet.create

    def fail_revision_create(self, **kwargs: Any):
        if self.model is RayTargetRouteRevision:
            raise IntegrityError(poison)
        return original_create(self, **kwargs)

    monkeypatch.setattr(QuerySet, "create", fail_revision_create)
    with pytest.raises(RayTargetRoutePersistenceRaceError) as error:
        _register_route()

    assert str(error.value) == "Ray target route could not serialize the mutation"
    assert poison not in str(error.value)
    assert not RayTargetRoute.objects.exists()


def test_connection_failures_are_mapped_without_private_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poison = "private-database-endpoint-secret"

    def fail_autocommit() -> bool:
        raise OperationalError(poison)

    monkeypatch.setattr(connections["default"], "get_autocommit", fail_autocommit)
    with pytest.raises(RayTargetRoutePersistenceRaceError) as error:
        register_ray_target_route(
            "ray-default",
            "target.primary",
            expected_target_policy_revision=2,
        )

    assert str(error.value) == "Ray target route could not serialize the mutation"
    assert poison not in str(error.value)


def test_public_surface_is_database_only_and_has_no_task_selection_writer() -> None:
    assert set(signature(register_ray_target_route).parameters) == {
        "backend_alias",
        "target_key",
        "expected_target_policy_revision",
        "using",
    }
    assert set(signature(transition_ray_target_route).parameters) == {
        "backend_alias",
        "target_key",
        "expected_route_revision",
        "expected_target_policy_revision",
        "using",
    }
    assert "RayTaskTargetRouteSelection" not in vars(routing)
    assert "probe_ray_target" not in vars(routing)
    assert "ray" not in vars(routing)
