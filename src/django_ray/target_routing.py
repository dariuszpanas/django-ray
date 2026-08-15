"""Dormant, database-only coordination for backend-to-target routes.

Every mutation owns one outermost durable transaction and acquires locks in
the fixed order ``route -> candidate target``.  PostgreSQL serializes the
route namespace and locks the stable route row; SQLite takes an exact no-op
update on that row as its first statement, which is also the database writer
fence.  Route revisions remain immutable history -- there is no mutable head
pointer, task-selection writer, target probe, Ray connection, or capacity
claim in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, transaction
from django.db.models import F
from django.db.utils import ConnectionDoesNotExist

from django_ray.models import (
    RayTarget,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayTargetRoute,
    RayTargetRouteRevision,
)
from django_ray.target_attestation import (
    RAY_TARGET_ATTESTATION_MAX_COUNTER,
    RayRunnerFamily,
    RayTargetExpectation,
)
from django_ray.target_coordination import (
    RayTargetCoordinationError,
    _latest_policy,
    _locked_target,
)

_SUPPORTED_DATABASE_VENDORS = frozenset({"postgresql", "sqlite"})
_CANONICAL_ROUTE_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_MAX_REVISION = RAY_TARGET_ATTESTATION_MAX_COUNTER
_POSTGRESQL_ROUTE_LOCK_DOMAIN = "django-ray:target-route:"


class RayTargetRoutingError(RuntimeError):
    """Base class for a fixed, redacted route-coordination refusal."""


class UnsupportedRayTargetRouteDatabaseError(RayTargetRoutingError):
    """The selected database cannot serialize route coordination."""

    def __init__(self) -> None:
        super().__init__("Ray target routing supports only SQLite and PostgreSQL")


class NestedRayTargetRouteTransactionError(RayTargetRoutingError):
    """A caller-owned transaction would weaken the durable boundary."""

    def __init__(self) -> None:
        super().__init__("Ray target routing must own the outermost database transaction")


class InvalidRayTargetRouteArgumentError(RayTargetRoutingError):
    """A public argument is not an exact bounded routing value."""

    def __init__(self) -> None:
        super().__init__("Ray target routing received an invalid argument")


class RayTargetRouteRegistrationConflictError(RayTargetRoutingError):
    """A stable route namespace is already registered differently."""

    def __init__(self) -> None:
        super().__init__("Ray target route registration conflicts with durable state")


class RayTargetRouteNotFoundError(RayTargetRoutingError):
    """No stable route exists for a requested transition."""

    def __init__(self) -> None:
        super().__init__("Ray target routing could not find the route")


class RayTargetRouteRevisionConflictError(RayTargetRoutingError):
    """The caller's reviewed route revision is stale."""

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__("Ray target route revision changed")


class RayTargetRoutePolicyRevisionConflictError(RayTargetRoutingError):
    """The caller's reviewed candidate-policy revision is stale."""

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__("Ray target route policy revision changed")


class RayTargetRouteRevisionExhaustedError(RayTargetRoutingError):
    """The route cannot append another signed-bigint revision."""

    def __init__(self) -> None:
        super().__init__("Ray target route revision is exhausted")


class RayTargetRouteStateError(RayTargetRoutingError):
    """Retained route history is absent, gapped, or otherwise invalid."""

    def __init__(self) -> None:
        super().__init__("Ray target route state is unavailable or invalid")


class RayTargetRoutePolicyStateError(RayTargetRoutingError):
    """The candidate target or its retained policy history is missing or invalid."""

    def __init__(self) -> None:
        super().__init__("Ray target route policy is unavailable or invalid")


class RayTargetRouteTargetNotActiveError(RayTargetRoutingError):
    """The exact latest candidate target policy is not active."""

    def __init__(self) -> None:
        super().__init__("Ray target route candidate is not active")


class RayJobTargetRoutingUnsupportedError(RayTargetRoutingError):
    """Ray Job lacks the authenticated routing boundary required here."""

    def __init__(self) -> None:
        super().__init__("Ray Job target routing is not supported")


class RayTargetRoutePersistenceRaceError(RayTargetRoutingError):
    """A database race or invariant prevented an append-only mutation."""

    def __init__(self) -> None:
        super().__init__("Ray target route could not serialize the mutation")


@dataclass(frozen=True, slots=True)
class RayTargetRouteChange:
    """Bounded result of route registration or one route transition."""

    backend_alias: str
    target_key: str
    target_policy_revision: int
    changed: bool
    previous_revision: int
    revision: int
    expectation: RayTargetExpectation


def _canonical_name(value: object) -> str:
    if type(value) is not str or _CANONICAL_ROUTE_NAME.fullmatch(value) is None:
        raise InvalidRayTargetRouteArgumentError
    return value


def _positive_revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise InvalidRayTargetRouteArgumentError
    return value


def _database_vendor(*, using: str) -> str:
    if type(using) is not str or not using:
        raise UnsupportedRayTargetRouteDatabaseError
    try:
        vendor = connections[using].vendor
    except (ConnectionDoesNotExist, TypeError, ValueError):
        raise UnsupportedRayTargetRouteDatabaseError from None
    if vendor not in _SUPPORTED_DATABASE_VENDORS:
        raise UnsupportedRayTargetRouteDatabaseError
    return vendor


def _require_outermost_transaction(*, using: str) -> None:
    database_connection = connections[using]
    if database_connection.in_atomic_block or not database_connection.get_autocommit():
        raise NestedRayTargetRouteTransactionError


def _postgresql_route_namespace_lock(*, backend_alias: str, using: str) -> None:
    """Serialize creation as well as updates for one PostgreSQL route key."""

    with connections[using].cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"{_POSTGRESQL_ROUTE_LOCK_DOMAIN}{backend_alias}"],
        )


def _locked_or_absent_route(
    *,
    backend_alias: str,
    using: str,
    vendor: str,
) -> RayTargetRoute | None:
    if vendor == "sqlite":
        updated = (
            RayTargetRoute.objects.using(using)
            .filter(backend_alias=backend_alias)
            .update(backend_alias=F("backend_alias"))
        )
        if updated == 0:
            return None
        if updated != 1:
            raise RayTargetRouteStateError
        try:
            return RayTargetRoute.objects.using(using).get(backend_alias=backend_alias)
        except RayTargetRoute.DoesNotExist:
            raise RayTargetRouteStateError from None

    _postgresql_route_namespace_lock(backend_alias=backend_alias, using=using)
    return (
        RayTargetRoute.objects.using(using)
        .select_for_update()
        .filter(backend_alias=backend_alias)
        .first()
    )


def _locked_candidate_target(*, target_key: str, using: str, vendor: str) -> RayTarget:
    try:
        return _locked_target(target_key=target_key, using=using, vendor=vendor)
    except RayTargetCoordinationError:
        raise RayTargetRoutePolicyStateError from None


def _active_candidate_policy(
    target: RayTarget,
    *,
    expected_revision: int,
    using: str,
) -> tuple[RayTargetPolicyRevision, RayTargetExpectation]:
    try:
        policy, expectation, desired_state = _latest_policy(target, using=using)
    except RayTargetCoordinationError:
        raise RayTargetRoutePolicyStateError from None

    actual_revision = int(policy.revision)
    if actual_revision != expected_revision:
        raise RayTargetRoutePolicyRevisionConflictError(
            expected_revision=expected_revision,
            actual_revision=actual_revision,
        )
    if expectation.runner_family is not RayRunnerFamily.RAY_CORE:
        raise RayJobTargetRoutingUnsupportedError
    if desired_state is not RayTargetDesiredState.ACTIVE:
        raise RayTargetRouteTargetNotActiveError
    return policy, expectation


def _latest_route_revision(
    route: RayTargetRoute,
    *,
    using: str,
) -> RayTargetRouteRevision:
    revisions = RayTargetRouteRevision.objects.using(using).filter(route_id=route.pk)
    latest = revisions.order_by("-revision").first()
    if latest is None:
        raise RayTargetRouteStateError
    revision = int(latest.revision)
    if revision < 1 or revision > _MAX_REVISION or revisions.count() != revision:
        raise RayTargetRouteStateError
    return latest


def _result(
    *,
    backend_alias: str,
    target_key: str,
    policy: RayTargetPolicyRevision,
    expectation: RayTargetExpectation,
    changed: bool,
    previous_revision: int,
    revision: int,
) -> RayTargetRouteChange:
    return RayTargetRouteChange(
        backend_alias=backend_alias,
        target_key=target_key,
        target_policy_revision=int(policy.revision),
        changed=changed,
        previous_revision=previous_revision,
        revision=revision,
        expectation=expectation,
    )


def register_ray_target_route(
    backend_alias: str,
    target_key: str,
    *,
    expected_target_policy_revision: int,
    using: str = DEFAULT_DB_ALIAS,
) -> RayTargetRouteChange:
    """Register route revision 1 or repeat that exact valid registration."""

    backend_alias = _canonical_name(backend_alias)
    target_key = _canonical_name(target_key)
    expected_target_policy_revision = _positive_revision(expected_target_policy_revision)
    try:
        vendor = _database_vendor(using=using)
        _require_outermost_transaction(using=using)
        with transaction.atomic(using=using, durable=True):
            route = _locked_or_absent_route(
                backend_alias=backend_alias,
                using=using,
                vendor=vendor,
            )
            created_route = route is None
            if route is None:
                route = RayTargetRoute.objects.using(using).create(
                    backend_alias=backend_alias,
                )

            target = _locked_candidate_target(
                target_key=target_key,
                using=using,
                vendor=vendor,
            )
            latest = None if created_route else _latest_route_revision(route, using=using)
            policy, expectation = _active_candidate_policy(
                target,
                expected_revision=expected_target_policy_revision,
                using=using,
            )

            if latest is not None:
                if int(latest.revision) == 1 and latest.target_policy_id == policy.pk:
                    return _result(
                        backend_alias=backend_alias,
                        target_key=target_key,
                        policy=policy,
                        expectation=expectation,
                        changed=False,
                        previous_revision=1,
                        revision=1,
                    )
                raise RayTargetRouteRegistrationConflictError

            RayTargetRouteRevision.objects.using(using).create(
                route=route,
                revision=1,
                target_policy=policy,
            )
            return _result(
                backend_alias=backend_alias,
                target_key=target_key,
                policy=policy,
                expectation=expectation,
                changed=True,
                previous_revision=0,
                revision=1,
            )
    except RayTargetRoutingError:
        raise
    except DatabaseError:
        raise RayTargetRoutePersistenceRaceError from None


def transition_ray_target_route(
    backend_alias: str,
    target_key: str,
    *,
    expected_route_revision: int,
    expected_target_policy_revision: int,
    using: str = DEFAULT_DB_ALIAS,
) -> RayTargetRouteChange:
    """CAS-append a route revision selecting one exact active target policy."""

    backend_alias = _canonical_name(backend_alias)
    target_key = _canonical_name(target_key)
    expected_route_revision = _positive_revision(expected_route_revision)
    expected_target_policy_revision = _positive_revision(expected_target_policy_revision)
    try:
        vendor = _database_vendor(using=using)
        _require_outermost_transaction(using=using)
        with transaction.atomic(using=using, durable=True):
            route = _locked_or_absent_route(
                backend_alias=backend_alias,
                using=using,
                vendor=vendor,
            )
            if route is None:
                raise RayTargetRouteNotFoundError

            target = _locked_candidate_target(
                target_key=target_key,
                using=using,
                vendor=vendor,
            )
            latest = _latest_route_revision(route, using=using)
            actual_route_revision = int(latest.revision)
            if actual_route_revision != expected_route_revision:
                raise RayTargetRouteRevisionConflictError(
                    expected_revision=expected_route_revision,
                    actual_revision=actual_route_revision,
                )
            policy, expectation = _active_candidate_policy(
                target,
                expected_revision=expected_target_policy_revision,
                using=using,
            )

            if latest.target_policy_id == policy.pk:
                return _result(
                    backend_alias=backend_alias,
                    target_key=target_key,
                    policy=policy,
                    expectation=expectation,
                    changed=False,
                    previous_revision=actual_route_revision,
                    revision=actual_route_revision,
                )
            if actual_route_revision >= _MAX_REVISION:
                raise RayTargetRouteRevisionExhaustedError

            next_revision = actual_route_revision + 1
            RayTargetRouteRevision.objects.using(using).create(
                route=route,
                revision=next_revision,
                target_policy=policy,
            )
            return _result(
                backend_alias=backend_alias,
                target_key=target_key,
                policy=policy,
                expectation=expectation,
                changed=True,
                previous_revision=actual_route_revision,
                revision=next_revision,
            )
    except RayTargetRoutingError:
        raise
    except DatabaseError:
        raise RayTargetRoutePersistenceRaceError from None


__all__ = [
    "InvalidRayTargetRouteArgumentError",
    "NestedRayTargetRouteTransactionError",
    "RayJobTargetRoutingUnsupportedError",
    "RayTargetRouteChange",
    "RayTargetRouteNotFoundError",
    "RayTargetRoutePersistenceRaceError",
    "RayTargetRoutePolicyRevisionConflictError",
    "RayTargetRoutePolicyStateError",
    "RayTargetRouteRegistrationConflictError",
    "RayTargetRouteRevisionConflictError",
    "RayTargetRouteRevisionExhaustedError",
    "RayTargetRouteStateError",
    "RayTargetRouteTargetNotActiveError",
    "RayTargetRoutingError",
    "UnsupportedRayTargetRouteDatabaseError",
    "register_ray_target_route",
    "transition_ray_target_route",
]
