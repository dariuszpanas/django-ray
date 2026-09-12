"""Private caller-transaction persistence for protocol-3 producer intent.

No released producer calls this seam. A future producer must validate its pure
intent before input preparation, then insert execution and intent in the same
transaction. Missing intent never authorizes a claim or selects a target.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections
from django.db.models import F

from django_ray.execution_protocol import COHORT_EXECUTION_PROTOCOL_VERSION
from django_ray.models import RayTaskCohortIntent, RayTaskExecution, TaskState
from django_ray.target.capabilities import RayWorkerTargetCapabilityError, _database_vendor, _now
from django_ray.target.cohort_intent import (
    COHORT_INTENT_SCHEMA_VERSION,
    CohortIntent,
    CohortIntentError,
    CohortSelectionPolicy,
    decode_cohort_intent,
    encode_cohort_intent,
)

_PRISTINE_NULL_FIELDS = (
    "claimed_by_worker",
    "started_at",
    "finished_at",
    "last_heartbeat_at",
    "ray_job_id",
    "ray_address",
    "ray_job_request_reference",
    "completion_data",
    "executor_django_ray_version",
    "result_data",
    "result_reference",
    "cancellation_status",
)


class CohortIntentStorageRejection(StrEnum):
    INVALID = "invalid"
    TRANSACTION_REQUIRED = "transaction_required"
    EXECUTION_UNAVAILABLE = "execution_unavailable"
    INTENT_UNAVAILABLE = "intent_unavailable"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    PERSISTENCE_REFUSED = "persistence_refused"


class CohortIntentStorageError(RuntimeError):
    """Fixed refusal without task arguments or backend configuration."""

    def __init__(self, classification: CohortIntentStorageRejection) -> None:
        self.classification = classification
        super().__init__(f"Cohort intent storage rejected: {classification.value}")


def _execution_id(value: object) -> int:
    if type(value) is not int or not 1 <= value <= (1 << 63) - 1:
        raise CohortIntentStorageError(CohortIntentStorageRejection.INVALID)
    return value


def persist_cohort_intent(
    execution_id: int,
    intent: CohortIntent,
    *,
    now: datetime,
    using: str = DEFAULT_DB_ALIAS,
) -> RayTaskCohortIntent:
    """Create immutable intent in the transaction that creates its execution."""
    execution_id = _execution_id(execution_id)
    try:
        canonical = decode_cohort_intent(encode_cohort_intent(intent))
        now = _now(now)
        vendor = _database_vendor(using=using)
    except (CohortIntentError, RayWorkerTargetCapabilityError):
        raise CohortIntentStorageError(CohortIntentStorageRejection.INVALID) from None
    if not connections[using].in_atomic_block:
        raise CohortIntentStorageError(CohortIntentStorageRejection.TRANSACTION_REQUIRED)
    try:
        execution_query = RayTaskExecution.objects.using(using).filter(pk=execution_id)
        if vendor == "sqlite":
            execution_query.update(task_id=F("task_id"))
            execution = execution_query.first()
        else:
            execution = execution_query.select_for_update().first()
        if execution is None or (
            execution.execution_protocol_version != COHORT_EXECUTION_PROTOCOL_VERSION
            or now < execution.created_at
            or execution.state != TaskState.QUEUED
            or execution.execution_generation != 0
            or execution.attempt_number != 1
            or any(getattr(execution, field) is not None for field in _PRISTINE_NULL_FIELDS)
        ):
            raise CohortIntentStorageError(CohortIntentStorageRejection.EXECUTION_UNAVAILABLE)
        return RayTaskCohortIntent.objects.using(using).create(
            execution_id=execution.pk,
            schema_version=COHORT_INTENT_SCHEMA_VERSION,
            package_version=canonical.package_version,
            backend_alias=canonical.backend_alias,
            configuration_digest=canonical.configuration_digest,
            runtime_env_identity_digest=canonical.runtime_env_identity_digest,
            selection_policy=canonical.selection_policy.value,
            created_at=now,
        )
    except DatabaseError:
        raise CohortIntentStorageError(CohortIntentStorageRejection.PERSISTENCE_REFUSED) from None


def read_cohort_intent(execution_id: int, *, using: str = DEFAULT_DB_ALIAS) -> CohortIntent:
    """Read validated current intent; unknown or absent storage fails closed."""
    execution_id = _execution_id(execution_id)
    try:
        _database_vendor(using=using)
        row = (
            RayTaskCohortIntent.objects.using(using)
            .select_related("execution")
            .filter(execution_id=execution_id)
            .first()
        )
        if row is None:
            raise CohortIntentStorageError(CohortIntentStorageRejection.INTENT_UNAVAILABLE)
        if row.schema_version != COHORT_INTENT_SCHEMA_VERSION or (
            row.execution.execution_protocol_version != COHORT_EXECUTION_PROTOCOL_VERSION
        ):
            raise CohortIntentStorageError(CohortIntentStorageRejection.UNSUPPORTED_SCHEMA)
        if row.created_at < row.execution.created_at:
            raise CohortIntentStorageError(CohortIntentStorageRejection.INVALID)
        return decode_cohort_intent(
            encode_cohort_intent(
                CohortIntent(
                    package_version=row.package_version,
                    backend_alias=row.backend_alias,
                    configuration_digest=row.configuration_digest,
                    runtime_env_identity_digest=row.runtime_env_identity_digest,
                    selection_policy=CohortSelectionPolicy(row.selection_policy),
                )
            )
        )
    except (CohortIntentError, ValueError, RayWorkerTargetCapabilityError):
        raise CohortIntentStorageError(CohortIntentStorageRejection.INVALID) from None
    except DatabaseError:
        raise CohortIntentStorageError(CohortIntentStorageRejection.PERSISTENCE_REFUSED) from None
