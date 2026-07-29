"""Bounded, authorized public reads for durable workflow progress."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NoReturn, cast
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import BinaryField, Case, Exists, F, OuterRef, Q, TextField, Value, When
from django.utils.crypto import constant_time_compare, salted_hmac

from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    WorkflowProgressNodeDetail,
    WorkflowProgressNodeState,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologySlot,
)
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow_plans import (
    WorkflowPlanValidationError,
    effective_plan_selection_reporting_policy,
)
from django_ray.workflow_progress import (
    MAX_PLAN_SELECTION_BYTES,
    WorkflowProgressDiagnosticCode,
    WorkflowProgressReadSource,
    _OctetLength,
    read_workflow_progress,
)
from django_ray.workflow_progress_storage import (
    WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES,
    WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES,
    WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES,
    WorkflowProgressStorageIntegrityError,
    WorkflowProgressTopologyCollection,
    _BlobOctetLength,
    _decode_truncation_reasons,
    verify_workflow_progress_node_detail_record,
    verify_workflow_progress_topology_manifest_record,
    verify_workflow_progress_topology_page_record,
)
from django_ray.workflow_progress_summary import (
    WORKFLOW_PROGRESS_STATES,
    WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES,
    WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
    WORKFLOW_PROGRESS_TERMINAL_STATES,
    WorkflowProgressDetailAvailability,
    WorkflowProgressSummaryError,
    WorkflowProgressTruncationReason,
    deserialize_workflow_progress_summary,
    public_workflow_progress_summary,
    serialize_workflow_progress_summary,
    workflow_progress_detail_is_last_observed,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


WORKFLOW_PROGRESS_READ_SCHEMA_VERSION = 1
WORKFLOW_PROGRESS_READ_DEFAULT_LIMIT = 100
WORKFLOW_PROGRESS_READ_MAX_LIMIT = 256
WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES = 512 * 1024
WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES = 1024 * 1024
WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES = 2 * 1024

_CURSOR_SALT = "django-ray.workflow-progress.cursor.v1"
_CURSOR_OWNER_SALT = "django-ray.workflow-progress.cursor-owner.v1"
_CURSOR_RUN_SALT = "django-ray.workflow-progress.cursor-run.v1"
_CURSOR_FIELDS = frozenset(
    {
        "v",
        "owner",
        "run",
        "run_identity",
        "summary_revision",
        "topology_version",
        "detail_revision",
        "collection",
        "filters",
        "order",
        "after",
        "position",
        "limit",
        "seen",
    }
)
_PAGE_COLLECTIONS = frozenset({"topology_nodes", "topology_edges", "node_details"})
_NORMAL_AVAILABILITIES = frozenset(
    {
        WorkflowProgressDetailAvailability.NOT_REPORTED.value,
        WorkflowProgressDetailAvailability.AVAILABLE.value,
        WorkflowProgressDetailAvailability.TRUNCATED.value,
        WorkflowProgressDetailAvailability.OMITTED_BY_POLICY.value,
        WorkflowProgressDetailAvailability.DISABLED.value,
        WorkflowProgressDetailAvailability.EXPIRED.value,
    }
)
_MAX_PUBLIC_COUNTER = (1 << 63) - 1
_DETAIL_COUNT_FIELD_BY_STATE = {
    WorkflowProgressNodeState.PENDING.value: "detail_pending_count",
    WorkflowProgressNodeState.RUNNING.value: "detail_running_count",
    WorkflowProgressNodeState.SUCCEEDED.value: "detail_succeeded_count",
    WorkflowProgressNodeState.FAILED.value: "detail_failed_count",
}
_DETAIL_TRUNCATION_REASONS = frozenset(
    {
        WorkflowProgressTruncationReason.DETAIL_COUNT_LIMIT.value,
        WorkflowProgressTruncationReason.DETAIL_ENCODED_BYTES.value,
        WorkflowProgressTruncationReason.DETAIL_DECODED_BYTES.value,
        WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value,
        WorkflowProgressTruncationReason.REPORTING_POLICY.value,
    }
)
_TOPOLOGY_TRUNCATION_REASONS = frozenset(
    {
        WorkflowProgressTruncationReason.NODE_COUNT_LIMIT.value,
        WorkflowProgressTruncationReason.EDGE_COUNT_LIMIT.value,
        WorkflowProgressTruncationReason.TOPOLOGY_ENCODED_BYTES.value,
        WorkflowProgressTruncationReason.TOPOLOGY_DECODED_BYTES.value,
        WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value,
    }
)
_DETAIL_EPOCH_FIELDS = (
    "detail_revision",
    "detail_node_count",
    *_DETAIL_COUNT_FIELD_BY_STATE.values(),
    "detail_truncation_reasons",
)
_DETAIL_TOPOLOGY_REASONS_FIELD = "topology_manifests__truncation_reasons"
_DETAIL_TOPOLOGY_REASONS_KEY = "topology_truncation_reasons"


class WorkflowProgressReadErrorCode(StrEnum):
    """Stable public failure codes for workflow-progress reads."""

    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_CURSOR = "INVALID_CURSOR"
    CURSOR_MISMATCH = "CURSOR_MISMATCH"
    MISSING = "MISSING"
    CORRUPT = "CORRUPT"


_ERROR_MESSAGES = {
    WorkflowProgressReadErrorCode.ACCESS_DENIED: "Workflow progress access is denied.",
    WorkflowProgressReadErrorCode.NOT_FOUND: "Workflow progress subject was not found.",
    WorkflowProgressReadErrorCode.INVALID_ARGUMENT: "Workflow progress read arguments are invalid.",
    WorkflowProgressReadErrorCode.INVALID_CURSOR: "Workflow progress cursor is invalid.",
    WorkflowProgressReadErrorCode.CURSOR_MISMATCH: (
        "Workflow progress cursor does not match this request."
    ),
    WorkflowProgressReadErrorCode.MISSING: "Referenced workflow progress storage is missing.",
    WorkflowProgressReadErrorCode.CORRUPT: "Workflow progress storage failed validation.",
}


class WorkflowProgressReadError(RuntimeError):
    """A bounded public workflow-progress read failure."""

    def __init__(self, code: WorkflowProgressReadErrorCode) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True)
class _SummaryContext:
    execution: RayTaskExecution
    selected_attempt_number: int
    source_schema_version: int | None
    summary: dict[str, Any] | None
    public_summary: dict[str, Any] | None
    identity: WorkflowRunIdentity | None
    availability: str
    complete: bool


def _raise(code: WorkflowProgressReadErrorCode, cause: Exception | None = None) -> NoReturn:
    error = WorkflowProgressReadError(code)
    if cause is None:
        raise error
    raise error from cause


def _database_for(execution: Any) -> str:
    state = getattr(execution, "_state", None)
    return str(getattr(state, "db", None) or "default")


def _execution_pk(execution: Any) -> int:
    value = getattr(execution, "pk", None)
    if type(value) is not int or not 1 <= value <= _MAX_PUBLIC_COUNTER:
        _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
    return cast(int, value)


def _applied_limit(value: Any) -> int:
    if type(value) is not int or value <= 0:
        _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
    return min(value, WORKFLOW_PROGRESS_READ_MAX_LIMIT)


def _selected_attempt(value: Any, execution: RayTaskExecution) -> int:
    if value is None:
        current = execution.attempt_number
        if type(current) is not int or not 1 <= current <= _MAX_PUBLIC_COUNTER:
            _raise(WorkflowProgressReadErrorCode.CORRUPT)
        return cast(int, current)
    if type(value) is not int or not 1 <= value <= _MAX_PUBLIC_COUNTER:
        _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
    return value


def _load_authorized_execution(
    execution: Any,
    *,
    authorize: Callable[[RayTaskExecution], bool],
    lock: bool,
) -> RayTaskExecution:
    if not callable(authorize):
        _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
    database = _database_for(execution)
    queryset = (
        RayTaskExecution.objects.using(database)
        .annotate(_summary_bytes=_OctetLength("workflow_progress_summary_json"))
        .annotate(
            _bounded_summary=Case(
                When(
                    _summary_bytes__lte=WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES,
                    then=F("workflow_progress_summary_json"),
                ),
                default=Value(None),
                output_field=TextField(),
            )
        )
    )
    if lock:
        queryset = queryset.select_for_update()
    try:
        fresh = queryset.only(
            "pk",
            "task_id",
            "callable_path",
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
        ).get(pk=_execution_pk(execution))
    except RayTaskExecution.DoesNotExist as error:
        _raise(WorkflowProgressReadErrorCode.NOT_FOUND, error)
    try:
        allowed = authorize(fresh)
    except Exception as error:  # pragma: no branch - deliberately collapses policy failures
        _raise(WorkflowProgressReadErrorCode.ACCESS_DENIED, error)
    if allowed is not True:
        _raise(WorkflowProgressReadErrorCode.ACCESS_DENIED)
    return fresh


def _bounded_summary_projection(queryset: Any) -> dict[str, Any] | None:
    return (
        queryset.annotate(_summary_bytes=_OctetLength("workflow_progress_summary_json"))
        .annotate(
            _bounded_summary=Case(
                When(
                    _summary_bytes__lte=WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES,
                    then=F("workflow_progress_summary_json"),
                ),
                default=Value(None),
                output_field=TextField(),
            )
        )
        .values("_summary_bytes", "_bounded_summary")
        .first()
    )


def _validated_summary_text(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None or row["_summary_bytes"] is None:
        return None
    octets = row["_summary_bytes"]
    text = row["_bounded_summary"]
    if (
        type(octets) is not int
        or octets <= 0
        or octets > WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES
        or not isinstance(text, str)
    ):
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    try:
        summary = deserialize_workflow_progress_summary(text)
        if serialize_workflow_progress_summary(summary) != text:
            raise WorkflowProgressSummaryError("summary is not canonical")
    except WorkflowProgressSummaryError as error:
        _raise(WorkflowProgressReadErrorCode.CORRUPT, error)
    return summary


def _read_schema_v3_summary(
    execution: RayTaskExecution,
    *,
    selected_attempt_number: int,
    using: str,
) -> dict[str, Any] | None:
    if selected_attempt_number == execution.attempt_number:
        if hasattr(execution, "_summary_bytes"):
            row = {
                "_summary_bytes": execution._summary_bytes,
                "_bounded_summary": execution._bounded_summary,
            }
        else:
            row = _bounded_summary_projection(
                RayTaskExecution.objects.using(using).filter(pk=execution.pk)
            )
    else:
        attempt_query = TaskAttempt.objects.using(using).filter(
            execution_id=execution.pk,
            attempt_number=selected_attempt_number,
        )
        row = _bounded_summary_projection(attempt_query)
        if row is None:
            _raise(WorkflowProgressReadErrorCode.NOT_FOUND)
    summary = _validated_summary_text(row)
    if summary is None:
        return None
    identity = summary["run_identity"]
    if (
        identity["task_execution_pk"] != execution.pk
        or identity["attempt_number"] != selected_attempt_number
    ):
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    if selected_attempt_number == execution.attempt_number:
        if (
            identity["execution_generation"] != execution.execution_generation
            or execution.workflow_run_id is None
            or identity["run_id"] != str(execution.workflow_run_id)
        ):
            _raise(WorkflowProgressReadErrorCode.CORRUPT)
    return summary


def _parse_protocol_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _effective_v3_summary(
    summary: dict[str, Any],
    *,
    now: datetime,
) -> tuple[dict[str, Any], str, bool]:
    public = public_workflow_progress_summary(summary)
    availability = str(public["detail"]["availability"])
    expiry = _parse_protocol_time(public["retention"]["detail_expires_at"])
    if (
        expiry is not None
        and expiry <= now
        and availability
        in {
            WorkflowProgressDetailAvailability.AVAILABLE.value,
            WorkflowProgressDetailAvailability.TRUNCATED.value,
        }
    ):
        availability = WorkflowProgressDetailAvailability.EXPIRED.value
        public = copy.deepcopy(public)
        public["detail"] = {
            "availability": availability,
            "complete": False,
            "truncation_reasons": public["detail"]["truncation_reasons"],
        }
    return public, availability, availability == WorkflowProgressDetailAvailability.AVAILABLE


def _legacy_int(value: Any) -> int:
    if type(value) is not int or value < 0:
        return 0
    return min(value, _MAX_PUBLIC_COUNTER)


def _legacy_float(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            result = float(value)
        except OverflowError:
            return 0.0
        if math.isfinite(result):
            return min(100.0, max(0.0, result))
    return 0.0


def _legacy_timestamp(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            result = float(value)
        except OverflowError:
            return 0.0
        if math.isfinite(result) and 0.0 <= result <= _MAX_PUBLIC_COUNTER:
            return result
    return 0.0


def _public_legacy_summary(payload: dict[str, Any], schema_version: int) -> dict[str, Any]:
    run_identity = payload.get("run_identity")
    public_identity = None
    if (
        isinstance(run_identity, dict)
        and type(run_identity.get("schema_version")) is int
        and run_identity["schema_version"] == 1
        and _is_canonical_uuid(run_identity.get("run_id"))
        and type(run_identity.get("attempt_number")) is int
        and 1 <= run_identity["attempt_number"] <= _MAX_PUBLIC_COUNTER
        and type(run_identity.get("execution_generation")) is int
        and 0 <= run_identity["execution_generation"] <= _MAX_PUBLIC_COUNTER
    ):
        public_identity = {
            "schema_version": 1,
            "run_id": run_identity["run_id"],
            "attempt_number": run_identity["attempt_number"],
            "execution_generation": run_identity["execution_generation"],
        }
    total = _legacy_int(payload.get("total_nodes"))
    completed = _legacy_int(payload.get("completed_nodes"))
    failed = _legacy_int(payload.get("failed_nodes"))
    running = _legacy_int(payload.get("running_nodes"))
    pending = _legacy_int(payload.get("pending_nodes"))
    graph = payload.get("graph")
    edge_count = 0
    if isinstance(graph, dict) and isinstance(graph.get("edges"), list):
        edge_count = min(len(graph["edges"]), _MAX_PUBLIC_COUNTER)
    return {
        "schema_version": schema_version,
        "run_identity": public_identity,
        "revision": _legacy_int(payload.get("revision")),
        "state": (
            payload["state"]
            if isinstance(payload.get("state"), str)
            and payload["state"] in WORKFLOW_PROGRESS_STATES
            else "RUNNING"
        ),
        "node_counts": {
            "declared": total,
            "discovered": total,
            "retained_topology": 0,
            "retained_detail": 0,
            "pending": pending,
            "running": running,
            "succeeded": completed,
            "failed": failed,
        },
        "edge_counts": {
            "declared": edge_count,
            "discovered": edge_count,
            "retained_topology": 0,
        },
        "progress_percent": _legacy_float(payload.get("progress_percent")),
        "updated_at": _legacy_timestamp(payload.get("updated_at")),
        "detail": {
            "availability": WorkflowProgressDetailAvailability.NOT_REPORTED.value,
            "complete": False,
            "truncation_reasons": [],
        },
    }


def _schema_v3_context(
    execution: RayTaskExecution,
    *,
    selected_attempt_number: int,
    summary: dict[str, Any],
    generated_at: datetime,
) -> _SummaryContext:
    identity = WorkflowRunIdentity(
        task_execution_pk=summary["run_identity"]["task_execution_pk"],
        attempt_number=summary["run_identity"]["attempt_number"],
        execution_generation=summary["run_identity"]["execution_generation"],
        run_id=summary["run_identity"]["run_id"],
    )
    public, availability, complete = _effective_v3_summary(summary, now=generated_at)
    return _SummaryContext(
        execution=execution,
        selected_attempt_number=selected_attempt_number,
        source_schema_version=WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
        summary=summary,
        public_summary=public,
        identity=identity,
        availability=availability,
        complete=complete,
    )


def _summary_context(
    execution: RayTaskExecution,
    *,
    selected_attempt_number: int,
    include_legacy: bool,
    generated_at: datetime,
    using: str,
) -> _SummaryContext:
    summary = _read_schema_v3_summary(
        execution,
        selected_attempt_number=selected_attempt_number,
        using=using,
    )
    if summary is not None:
        return _schema_v3_context(
            execution,
            selected_attempt_number=selected_attempt_number,
            summary=summary,
            generated_at=generated_at,
        )

    if include_legacy and selected_attempt_number == execution.attempt_number:
        result = read_workflow_progress(execution)
        if result.diagnostic_code is not None:
            if result.diagnostic_code is WorkflowProgressDiagnosticCode.ROW_MISSING:
                _raise(WorkflowProgressReadErrorCode.NOT_FOUND)
            _raise(WorkflowProgressReadErrorCode.CORRUPT)
        if result.payload is not None and result.source is WorkflowProgressReadSource.SUMMARY:
            return _schema_v3_context(
                execution,
                selected_attempt_number=selected_attempt_number,
                summary=result.payload,
                generated_at=generated_at,
            )
        if result.payload is not None and result.source is WorkflowProgressReadSource.LEGACY:
            schema_version = result.schema_version or 1
            public = _public_legacy_summary(result.payload, schema_version)
            legacy_identity = None
            public_identity = public["run_identity"]
            if public_identity is not None:
                legacy_identity = WorkflowRunIdentity(
                    task_execution_pk=execution.pk,
                    attempt_number=public_identity["attempt_number"],
                    execution_generation=public_identity["execution_generation"],
                    run_id=public_identity["run_id"],
                )
                if (
                    legacy_identity.attempt_number != selected_attempt_number
                    or legacy_identity.execution_generation != execution.execution_generation
                    or execution.workflow_run_id is None
                    or legacy_identity.run_id != str(execution.workflow_run_id)
                ):
                    _raise(WorkflowProgressReadErrorCode.CORRUPT)
            return _SummaryContext(
                execution=execution,
                selected_attempt_number=selected_attempt_number,
                source_schema_version=schema_version,
                summary=None,
                public_summary=public,
                identity=legacy_identity,
                availability=WorkflowProgressDetailAvailability.NOT_REPORTED.value,
                complete=False,
            )

    availability = WorkflowProgressDetailAvailability.NOT_REPORTED.value
    if selected_attempt_number == execution.attempt_number:
        reporting_policy = _current_workflow_reporting_policy(execution, using=using)
        if reporting_policy == "disabled":
            availability = WorkflowProgressDetailAvailability.DISABLED.value
        elif reporting_policy == "full" and execution.state in WORKFLOW_PROGRESS_TERMINAL_STATES:
            availability = WorkflowProgressDetailAvailability.MISSING.value

    return _SummaryContext(
        execution=execution,
        selected_attempt_number=selected_attempt_number,
        source_schema_version=None,
        summary=None,
        public_summary=None,
        identity=None,
        availability=availability,
        complete=False,
    )


def _current_workflow_reporting_policy(
    execution: RayTaskExecution,
    *,
    using: str,
) -> str | None:
    """Read the current selection policy without loading an unbounded value."""
    row = (
        RayTaskExecution.objects.using(using)
        .filter(
            pk=execution.pk,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
            workflow_run_id=execution.workflow_run_id,
        )
        .annotate(_selection_bytes=_OctetLength("workflow_plan_selection"))
        .annotate(
            _bounded_selection=Case(
                When(
                    _selection_bytes__lte=MAX_PLAN_SELECTION_BYTES,
                    then=F("workflow_plan_selection"),
                ),
                default=Value(None),
                output_field=TextField(),
            )
        )
        .values("_selection_bytes", "_bounded_selection")
        .first()
    )
    if row is None or row["_selection_bytes"] is None:
        return None
    if (
        type(row["_selection_bytes"]) is not int
        or row["_selection_bytes"] <= 0
        or row["_selection_bytes"] > MAX_PLAN_SELECTION_BYTES
        or not isinstance(row["_bounded_selection"], str)
    ):
        return None
    try:
        selection = json.loads(row["_bounded_selection"])
        return effective_plan_selection_reporting_policy(selection)
    except (
        TypeError,
        RecursionError,
        json.JSONDecodeError,
        WorkflowPlanValidationError,
    ):
        return None


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _normalized_observed_at(value: Any) -> datetime:
    if not isinstance(value, datetime):
        _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    except (OverflowError, TypeError, ValueError) as error:
        _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT, error)


def _public_identity(identity: WorkflowRunIdentity | None) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "schema_version": 1,
        "run_id": identity.run_id,
        "attempt_number": identity.attempt_number,
        "execution_generation": identity.execution_generation,
    }


def _publication(context: _SummaryContext) -> dict[str, int | None]:
    summary = context.summary or {}
    return {
        "summary_revision": summary.get("summary_revision"),
        "topology_version": summary.get("topology_version"),
        "detail_revision": summary.get("detail_revision"),
    }


def _common_envelope(
    context: _SummaryContext,
    *,
    schema: str,
    generated_at: datetime,
) -> dict[str, Any]:
    return {
        "schema": f"django-ray.{schema}",
        "schema_version": WORKFLOW_PROGRESS_READ_SCHEMA_VERSION,
        "generated_at": _isoformat(generated_at),
        "task_id": context.execution.task_id,
        "run_identity": _public_identity(context.identity),
        "publication": _publication(context),
        "availability": context.availability,
        "complete": context.complete,
    }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _wire_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8")


def _secret_keys() -> tuple[str, ...]:
    return (settings.SECRET_KEY, *tuple(getattr(settings, "SECRET_KEY_FALLBACKS", ())))


def _hmac_values(salt: str, value: bytes) -> tuple[str, ...]:
    return tuple(
        salted_hmac(salt, value, secret=secret, algorithm="sha256").hexdigest()
        for secret in _secret_keys()
    )


def _owner_tags(execution_pk: int) -> tuple[str, ...]:
    return _hmac_values(_CURSOR_OWNER_SALT, str(execution_pk).encode("ascii"))


def _run_tags(identity: WorkflowRunIdentity) -> tuple[str, ...]:
    return _hmac_values(_CURSOR_RUN_SALT, _canonical_bytes(identity.as_dict()))


def _matches_tag(value: str, candidates: tuple[str, ...]) -> bool:
    return any(constant_time_compare(value, candidate) for candidate in candidates)


def _is_lower_hex(value: Any, *, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _cursor_public_identity(value: Any) -> dict[str, Any] | None:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "run_id", "attempt_number", "execution_generation"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or not _is_canonical_uuid(value.get("run_id"))
        or type(value.get("attempt_number")) is not int
        or not 1 <= value["attempt_number"] <= _MAX_PUBLIC_COUNTER
        or type(value.get("execution_generation")) is not int
        or not 0 <= value["execution_generation"] <= _MAX_PUBLIC_COUNTER
    ):
        return None
    return cast(dict[str, Any], value)


def _cursor_run_identity(execution_pk: int, value: Any) -> WorkflowRunIdentity | None:
    public = _cursor_public_identity(value)
    if public is None:
        return None
    return WorkflowRunIdentity(
        task_execution_pk=execution_pk,
        attempt_number=public["attempt_number"],
        execution_generation=public["execution_generation"],
        run_id=public["run_id"],
    )


def _cursor_payload(
    context: _SummaryContext,
    *,
    collection: str,
    filters: list[list[str]],
    order: str,
    after: str | list[str] | None,
    position: list[int] | None,
    limit: int,
    seen: int,
) -> dict[str, Any]:
    if context.identity is None or context.summary is None:
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    summary = context.summary
    detail_revision = summary["detail_revision"] if collection == "node_details" else None
    public_identity = _public_identity(context.identity)
    if public_identity is None:
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    return {
        "v": WORKFLOW_PROGRESS_READ_SCHEMA_VERSION,
        "owner": _owner_tags(context.execution.pk)[0],
        "run": _run_tags(context.identity)[0],
        "run_identity": public_identity,
        "summary_revision": summary["summary_revision"],
        "topology_version": summary["topology_version"],
        "detail_revision": detail_revision,
        "collection": collection,
        "filters": filters,
        "order": order,
        "after": after,
        "position": position,
        "limit": limit,
        "seen": seen,
    }


def _encode_cursor(payload: dict[str, Any]) -> str:
    cursor = signing.dumps(payload, salt=_CURSOR_SALT, compress=False)
    if len(cursor.encode("utf-8")) > WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES:
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    return cursor


def _cursor_length_is_valid(cursor: Any) -> bool:
    if cursor is None:
        return True
    if not isinstance(cursor, str) or not cursor:
        return False
    try:
        return len(cursor.encode("utf-8")) <= WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES
    except UnicodeEncodeError:
        return False


def _decode_cursor(
    cursor: str | None,
    *,
    context: _SummaryContext,
    collection: str,
    filters: list[list[str]],
    order: str,
    limit: int,
) -> tuple[dict[str, Any] | None, bool]:
    if cursor is None:
        return None, False
    try:
        value = signing.loads(cursor, salt=_CURSOR_SALT)
    except signing.BadSignature as error:
        _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR, error)
    if not isinstance(value, dict) or set(value) != _CURSOR_FIELDS:
        _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
    if (
        type(value["v"]) is not int
        or value["v"] != WORKFLOW_PROGRESS_READ_SCHEMA_VERSION
        or not _is_lower_hex(value["owner"], length=64)
        or not _is_lower_hex(value["run"], length=64)
        or _cursor_public_identity(value["run_identity"]) is None
        or type(value["summary_revision"]) is not int
        or not 1 <= value["summary_revision"] <= _MAX_PUBLIC_COUNTER
        or type(value["topology_version"]) is not int
        or not 1 <= value["topology_version"] <= _MAX_PUBLIC_COUNTER
        or (
            value["detail_revision"] is not None
            and (
                type(value["detail_revision"]) is not int
                or not 1 <= value["detail_revision"] <= _MAX_PUBLIC_COUNTER
            )
        )
        or value["collection"] not in _PAGE_COLLECTIONS
        or not isinstance(value["filters"], list)
        or not all(
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(part, str) for part in item)
            for item in value["filters"]
        )
        or not isinstance(value["order"], str)
        or type(value["limit"]) is not int
        or not 1 <= value["limit"] <= WORKFLOW_PROGRESS_READ_MAX_LIMIT
        or type(value["seen"]) is not int
        or not 0 <= value["seen"] <= _MAX_PUBLIC_COUNTER
    ):
        _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
    if not _matches_tag(value["owner"], _owner_tags(context.execution.pk)):
        _raise(WorkflowProgressReadErrorCode.CURSOR_MISMATCH)
    cursor_identity = _cursor_run_identity(context.execution.pk, value["run_identity"])
    if cursor_identity is None or not _matches_tag(value["run"], _run_tags(cursor_identity)):
        _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
    cursor_collection = value["collection"]
    if cursor_collection == "node_details":
        if (
            value["detail_revision"] is None
            or value["position"] is not None
            or not _is_lower_hex(value["after"], length=64)
        ):
            _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
    else:
        if value["detail_revision"] is not None:
            _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
        position = value["position"]
        if (
            not isinstance(position, list)
            or len(position) != 2
            or any(
                type(item) is not int or not 0 <= item <= _MAX_PUBLIC_COUNTER for item in position
            )
        ):
            _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
        if cursor_collection == "topology_nodes":
            if not isinstance(value["after"], str) or not value["after"]:
                _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
        elif (
            not isinstance(value["after"], list)
            or len(value["after"]) != 2
            or not all(isinstance(item, str) and item for item in value["after"])
        ):
            _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
    if (
        value["collection"] != collection
        or value["filters"] != filters
        or value["order"] != order
        or value["limit"] != limit
    ):
        _raise(WorkflowProgressReadErrorCode.CURSOR_MISMATCH)
    if context.identity is None or context.summary is None:
        return value, True
    expired = not _matches_tag(value["run"], _run_tags(context.identity))
    expired = expired or value["topology_version"] != context.summary["topology_version"]
    if collection == "node_details":
        expired = expired or value["detail_revision"] != context.summary["detail_revision"]
    elif value["detail_revision"] is not None:
        _raise(WorkflowProgressReadErrorCode.CURSOR_MISMATCH)
    return value, expired


def _empty_page(
    context: _SummaryContext,
    *,
    collection: str,
    generated_at: datetime,
) -> dict[str, Any]:
    response = {
        **_common_envelope(
            context,
            schema="workflow-progress-page",
            generated_at=generated_at,
        ),
        "collection": collection,
        "returned_count": 0,
        "items": [],
        "next_cursor": None,
    }
    if len(_wire_bytes(response)) > WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES:
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    return response


def _expired_page(
    context: _SummaryContext,
    cursor: Mapping[str, Any],
    *,
    collection: str,
    generated_at: datetime,
) -> dict[str, Any]:
    response = {
        "schema": "django-ray.workflow-progress-page",
        "schema_version": WORKFLOW_PROGRESS_READ_SCHEMA_VERSION,
        "generated_at": _isoformat(generated_at),
        "task_id": context.execution.task_id,
        "run_identity": copy.deepcopy(cursor["run_identity"]),
        "publication": {
            "summary_revision": cursor["summary_revision"],
            "topology_version": cursor["topology_version"],
            "detail_revision": cursor["detail_revision"],
        },
        "availability": WorkflowProgressDetailAvailability.EXPIRED.value,
        "complete": False,
        "collection": collection,
        "returned_count": 0,
        "items": [],
        "next_cursor": None,
    }
    if len(_wire_bytes(response)) > WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES:
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    return response


def _ensure_readable(context: _SummaryContext) -> bool:
    if context.availability == WorkflowProgressDetailAvailability.MISSING.value:
        _raise(WorkflowProgressReadErrorCode.MISSING)
    if context.availability == WorkflowProgressDetailAvailability.CORRUPT.value:
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    if context.availability not in _NORMAL_AVAILABILITIES:
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    return context.availability in {
        WorkflowProgressDetailAvailability.AVAILABLE.value,
        WorkflowProgressDetailAvailability.TRUNCATED.value,
    }


def _public_node_detail(value: dict[str, Any]) -> dict[str, Any]:
    public = copy.deepcopy(value)
    invocation = public.get("invocation_identity")
    if isinstance(invocation, dict):
        invocation.pop("task_execution_pk", None)
    return public


def _bounded_response(
    base: dict[str, Any],
    *,
    items: list[dict[str, Any]],
    cursor_for_count: Callable[[int], str | None],
) -> dict[str, Any]:
    selected = list(items)
    while True:
        next_cursor = cursor_for_count(len(selected))
        response = {
            **base,
            "returned_count": len(selected),
            "items": selected,
            "next_cursor": next_cursor,
        }
        if len(_wire_bytes(response)) <= WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES:
            return response
        if not selected:
            _raise(WorkflowProgressReadErrorCode.CORRUPT)
        selected.pop()


def get_workflow_progress_summary(
    execution: Any,
    *,
    authorize: Callable[[RayTaskExecution], bool],
    include_legacy: bool = False,
    attempt_number: int | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return the authorized bounded aggregate summary for one workflow run."""
    fresh = _load_authorized_execution(execution, authorize=authorize, lock=False)
    if type(include_legacy) is not bool:
        _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
    observed_at = _normalized_observed_at(
        datetime.now(UTC) if generated_at is None else generated_at
    )
    selected = _selected_attempt(attempt_number, fresh)
    context = _summary_context(
        fresh,
        selected_attempt_number=selected,
        include_legacy=include_legacy,
        generated_at=observed_at,
        using=_database_for(fresh),
    )
    response = {
        **_common_envelope(
            context,
            schema="workflow-progress-summary",
            generated_at=observed_at,
        ),
        "source_schema_version": context.source_schema_version,
        "summary": context.public_summary,
    }
    if len(_wire_bytes(response)) > WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES:
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    return response


def _locked_context(
    execution: Any,
    *,
    authorize: Callable[[RayTaskExecution], bool],
    attempt_number: int | None,
    generated_at: datetime,
) -> _SummaryContext:
    fresh = _load_authorized_execution(execution, authorize=authorize, lock=True)
    selected = _selected_attempt(attempt_number, fresh)
    return _summary_context(
        fresh,
        selected_attempt_number=selected,
        include_legacy=False,
        generated_at=generated_at,
        using=_database_for(fresh),
    )


def _manifest_row(context: _SummaryContext, *, using: str) -> dict[str, Any] | None:
    if context.identity is None or context.summary is None:
        return None
    node_collection = WorkflowProgressTopologyCollection.NODE.value
    edge_collection = WorkflowProgressTopologyCollection.EDGE.value
    unexpected_links = (
        WorkflowProgressTopologyManifestPage.objects.using(using)
        .filter(manifest_id=OuterRef("pk"))
        .filter(
            ~Q(collection__in=(node_collection, edge_collection))
            | Q(collection=node_collection, page_index__gte=OuterRef("node_page_count"))
            | Q(collection=edge_collection, page_index__gte=OuterRef("edge_page_count"))
        )
    )
    return (
        WorkflowProgressTopologyManifest.objects.using(using)
        .filter(
            run_storage__execution_id=context.identity.task_execution_pk,
            run_storage__attempt_number=context.identity.attempt_number,
            run_storage__execution_generation=context.identity.execution_generation,
            run_storage__run_id=context.identity.run_id,
            topology_version=context.summary["topology_version"],
            slot=WorkflowProgressTopologySlot.CURRENT,
        )
        .annotate(
            _payload_octets=_BlobOctetLength("payload"),
            _unexpected_link=Exists(unexpected_links),
        )
        .annotate(
            _bounded_payload=Case(
                When(
                    _payload_octets__lte=(WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES),
                    then=F("payload"),
                ),
                default=Value(None),
                output_field=BinaryField(),
            )
        )
        .values(
            "pk",
            "run_storage_id",
            "run_storage__execution_id",
            "run_storage__attempt_number",
            "run_storage__execution_generation",
            "run_storage__run_id",
            "run_storage__detail_truncation_reasons",
            "topology_version",
            "slot",
            "manifest_digest",
            "truncation_reasons",
            "node_count",
            "edge_count",
            "node_page_count",
            "edge_page_count",
            "encoded_bytes",
            "decoded_bytes",
            "published_at",
            "_payload_octets",
            "_bounded_payload",
            "_unexpected_link",
        )
        .first()
    )


def _topology_page_row(
    manifest_id: Any,
    *,
    collection: str,
    page_index: int,
    using: str,
) -> dict[str, Any] | None:
    return (
        WorkflowProgressTopologyManifestPage.objects.using(using)
        .filter(
            manifest_id=manifest_id,
            collection=collection,
            page_index=page_index,
        )
        .annotate(_payload_octets=_BlobOctetLength("page__payload"))
        .annotate(
            _bounded_payload=Case(
                When(
                    _payload_octets__lte=WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES,
                    then=F("page__payload"),
                ),
                default=Value(None),
                output_field=BinaryField(),
            )
        )
        .values(
            "collection",
            "page_index",
            "page_id",
            "page__run_storage_id",
            "page__digest",
            "page__collection",
            "page__encoding",
            "page__item_count",
            "page__encoded_bytes",
            "page__decoded_bytes",
            "_payload_octets",
            "_bounded_payload",
        )
        .first()
    )


def _topology_stable_key(collection: str, item: Mapping[str, Any]) -> str | list[str]:
    if collection == "topology_nodes":
        return str(item["node_id"])
    return [str(item["source"]), str(item["target"])]


def _stable_key_not_after(left: str | list[str], right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left <= right
    if isinstance(left, list) and isinstance(right, list):
        return left <= right
    return True


def _list_topology(
    execution: Any,
    *,
    authorize: Callable[[RayTaskExecution], bool],
    attempt_number: int | None,
    cursor: str | None,
    limit: int,
    public_collection: str,
    stored_collection: str,
    order: str,
) -> dict[str, Any]:
    observed_at = datetime.now(UTC)
    using = _database_for(execution)
    with transaction.atomic(using=using):
        context = _locked_context(
            execution,
            authorize=authorize,
            attempt_number=attempt_number,
            generated_at=observed_at,
        )
        applied = _applied_limit(limit)
        if not _cursor_length_is_valid(cursor):
            _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
        decoded_cursor, expired = _decode_cursor(
            cursor,
            context=context,
            collection=public_collection,
            filters=[],
            order=order,
            limit=applied,
        )
        if expired:
            if decoded_cursor is None:
                _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
            return _expired_page(
                context,
                decoded_cursor,
                collection=public_collection,
                generated_at=observed_at,
            )
        if (
            decoded_cursor is not None
            and context.availability == WorkflowProgressDetailAvailability.EXPIRED.value
        ):
            return _expired_page(
                context,
                decoded_cursor,
                collection=public_collection,
                generated_at=observed_at,
            )
        if not _ensure_readable(context):
            return _empty_page(
                context,
                collection=public_collection,
                generated_at=observed_at,
            )
        if context.summary is None or context.summary["topology_version"] is None:
            _raise(WorkflowProgressReadErrorCode.MISSING)
        summary = context.summary
        manifest_row = _manifest_row(context, using=using)
        if manifest_row is None:
            _raise(WorkflowProgressReadErrorCode.MISSING)
        try:
            manifest = verify_workflow_progress_topology_manifest_record(
                manifest_row,
                expected_identity=context.identity,
            )
        except WorkflowProgressStorageIntegrityError as error:
            _raise(WorkflowProgressReadErrorCode.CORRUPT, error)
        summary_reasons = set(summary["detail"]["truncation_reasons"])
        manifest_reasons = set(manifest.truncation_reasons)
        try:
            run_detail_reasons = set(
                _decode_truncation_reasons(
                    manifest_row["run_storage__detail_truncation_reasons"],
                    stored=True,
                )
            )
        except WorkflowProgressStorageIntegrityError as error:
            _raise(WorkflowProgressReadErrorCode.CORRUPT, error)
        expected_summary_reasons = manifest_reasons | run_detail_reasons
        if workflow_progress_detail_is_last_observed(summary):
            expected_summary_reasons.add(
                WorkflowProgressTruncationReason.TERMINAL_STATE_UNREPORTED.value
            )
        if (
            manifest.node_count != summary["node_counts"]["retained_topology"]
            or manifest.edge_count != summary["edge_counts"]["retained_topology"]
            or manifest.expected_link_count != len(manifest.page_descriptors)
            or manifest_row["_unexpected_link"] is not False
            or not manifest_reasons.issubset(_TOPOLOGY_TRUNCATION_REASONS)
            or not run_detail_reasons.issubset(_DETAIL_TRUNCATION_REASONS)
            or expected_summary_reasons != summary_reasons
            or (
                summary_reasons
                and context.availability != WorkflowProgressDetailAvailability.TRUNCATED.value
            )
        ):
            _raise(WorkflowProgressReadErrorCode.CORRUPT)

        descriptors = [
            descriptor
            for descriptor in manifest.page_descriptors
            if descriptor["collection"] == stored_collection
        ]
        page_index = 0
        offset = 0
        prior_seen = 0
        after: str | list[str] | None = None
        if decoded_cursor is not None:
            position = decoded_cursor["position"]
            after = decoded_cursor["after"]
            prior_seen = decoded_cursor["seen"]
            if (
                not isinstance(position, list)
                or len(position) != 2
                or any(type(item) is not int or item < 0 for item in position)
            ):
                _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
            page_index, offset = position
        if not descriptors:
            if decoded_cursor is not None:
                _raise(WorkflowProgressReadErrorCode.CURSOR_MISMATCH)
            return _empty_page(
                context,
                collection=public_collection,
                generated_at=observed_at,
            )
        descriptor_by_index = {item["page_index"]: item for item in descriptors}
        descriptor = descriptor_by_index.get(page_index)
        if descriptor is None:
            _raise(WorkflowProgressReadErrorCode.CURSOR_MISMATCH)
        expected_before = (
            sum(item["item_count"] for item in descriptors if item["page_index"] < page_index)
            + offset
        )
        if prior_seen != expected_before:
            _raise(WorkflowProgressReadErrorCode.CURSOR_MISMATCH)
        page_row = _topology_page_row(
            manifest_row["pk"],
            collection=stored_collection,
            page_index=page_index,
            using=using,
        )
        if page_row is None:
            _raise(WorkflowProgressReadErrorCode.MISSING)
        try:
            records = list(
                verify_workflow_progress_topology_page_record(
                    page_row,
                    descriptor=descriptor,
                    expected_run_storage_id=manifest_row["run_storage_id"],
                )
            )
        except WorkflowProgressStorageIntegrityError as error:
            _raise(WorkflowProgressReadErrorCode.CORRUPT, error)
        if offset > len(records):
            _raise(WorkflowProgressReadErrorCode.CURSOR_MISMATCH)
        if offset and _topology_stable_key(public_collection, records[offset - 1]) != after:
            _raise(WorkflowProgressReadErrorCode.CURSOR_MISMATCH)
        if (
            decoded_cursor is not None
            and offset == 0
            and records
            and _stable_key_not_after(
                _topology_stable_key(public_collection, records[0]),
                after,
            )
        ):
            _raise(WorkflowProgressReadErrorCode.CORRUPT)
        candidates: list[dict[str, Any]] = []
        decoded_bytes = 0
        for item in records[offset : offset + applied]:
            item_bytes = len(_canonical_bytes(item))
            if decoded_bytes + item_bytes > WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES:
                if not candidates:
                    _raise(WorkflowProgressReadErrorCode.CORRUPT)
                break
            candidates.append(copy.deepcopy(item))
            decoded_bytes += item_bytes
        later_page_indexes = sorted(index for index in descriptor_by_index if index > page_index)
        has_later_page = bool(later_page_indexes)
        next_page_index = later_page_indexes[0] if later_page_indexes else None
        expected_total = sum(item["item_count"] for item in descriptors)

        base = {
            **_common_envelope(
                context,
                schema="workflow-progress-page",
                generated_at=observed_at,
            ),
            "collection": public_collection,
        }

        def cursor_for_count(count: int) -> str | None:
            next_offset = offset + count
            more_here = next_offset < len(records)
            emitted = prior_seen + count
            if not more_here and not has_later_page:
                if emitted < expected_total:
                    _raise(WorkflowProgressReadErrorCode.MISSING)
                if emitted > expected_total:
                    _raise(WorkflowProgressReadErrorCode.CORRUPT)
                return None
            if count <= 0:
                _raise(WorkflowProgressReadErrorCode.CORRUPT)
            if emitted >= expected_total:
                _raise(WorkflowProgressReadErrorCode.CORRUPT)
            next_position = (
                [page_index, next_offset] if more_here else [cast(int, next_page_index), 0]
            )
            payload = _cursor_payload(
                context,
                collection=public_collection,
                filters=[],
                order=order,
                after=_topology_stable_key(public_collection, candidates[count - 1]),
                position=next_position,
                limit=applied,
                seen=emitted,
            )
            return _encode_cursor(payload)

        return _bounded_response(base, items=candidates, cursor_for_count=cursor_for_count)


def list_workflow_topology_nodes(
    execution: Any,
    *,
    authorize: Callable[[RayTaskExecution], bool],
    attempt_number: int | None = None,
    cursor: str | None = None,
    limit: int = WORKFLOW_PROGRESS_READ_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return one bounded page of immutable topology nodes."""
    return _list_topology(
        execution,
        authorize=authorize,
        attempt_number=attempt_number,
        cursor=cursor,
        limit=limit,
        public_collection="topology_nodes",
        stored_collection=WorkflowProgressTopologyCollection.NODE.value,
        order="node_id_asc",
    )


def list_workflow_topology_edges(
    execution: Any,
    *,
    authorize: Callable[[RayTaskExecution], bool],
    attempt_number: int | None = None,
    cursor: str | None = None,
    limit: int = WORKFLOW_PROGRESS_READ_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return one bounded page of immutable topology edges."""
    return _list_topology(
        execution,
        authorize=authorize,
        attempt_number=attempt_number,
        cursor=cursor,
        limit=limit,
        public_collection="topology_edges",
        stored_collection=WorkflowProgressTopologyCollection.EDGE.value,
        order="source_target_asc",
    )


def _normalized_state(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
    normalized = value.upper()
    if normalized not in WorkflowProgressNodeState.values:
        _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
    return normalized


def _detail_epoch_query(context: _SummaryContext, *, using: str) -> Any:
    if context.identity is None or context.summary is None:
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    summary = context.summary
    return WorkflowProgressNodeDetail.objects.using(using).filter(
        run_storage__execution_id=context.identity.task_execution_pk,
        run_storage__attempt_number=context.identity.attempt_number,
        run_storage__execution_generation=context.identity.execution_generation,
        run_storage__run_id=context.identity.run_id,
        run_storage__detail_revision=summary["detail_revision"],
        run_storage__topology_manifests__slot=WorkflowProgressTopologySlot.CURRENT,
        run_storage__topology_manifests__topology_version=summary["topology_version"],
    )


def _detail_epoch_record(
    context: _SummaryContext,
    *,
    using: str,
) -> dict[str, Any] | None:
    if context.identity is None or context.summary is None:
        return None
    row = (
        WorkflowProgressRunStorage.objects.using(using)
        .filter(
            execution_id=context.identity.task_execution_pk,
            attempt_number=context.identity.attempt_number,
            execution_generation=context.identity.execution_generation,
            run_id=context.identity.run_id,
            detail_revision=context.summary["detail_revision"],
            topology_manifests__slot=WorkflowProgressTopologySlot.CURRENT,
            topology_manifests__topology_version=context.summary["topology_version"],
        )
        .values(*_DETAIL_EPOCH_FIELDS, _DETAIL_TOPOLOGY_REASONS_FIELD)
        .first()
    )
    if row is None:
        return None
    row[_DETAIL_TOPOLOGY_REASONS_KEY] = row.pop(_DETAIL_TOPOLOGY_REASONS_FIELD)
    return row


def _expected_detail_count(
    context: _SummaryContext,
    record: Mapping[str, Any],
    *,
    state: str | None,
) -> int:
    if context.summary is None:
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    counts = {name: record.get(field) for name, field in _DETAIL_COUNT_FIELD_BY_STATE.items()}
    total = record.get("detail_node_count")
    summary_counts = context.summary["node_counts"]
    try:
        detail_reasons = set(
            _decode_truncation_reasons(
                record.get("detail_truncation_reasons"),
                stored=True,
            )
        )
        topology_reasons = set(
            _decode_truncation_reasons(
                record.get(_DETAIL_TOPOLOGY_REASONS_KEY),
                stored=True,
            )
        )
    except WorkflowProgressStorageIntegrityError as error:
        _raise(WorkflowProgressReadErrorCode.CORRUPT, error)
    last_observed_terminal_detail = workflow_progress_detail_is_last_observed(context.summary)
    expected_summary_reasons = detail_reasons | topology_reasons
    if last_observed_terminal_detail:
        expected_summary_reasons.add(
            WorkflowProgressTruncationReason.TERMINAL_STATE_UNREPORTED.value
        )
    if (
        type(total) is not int
        or total < 0
        or record.get("detail_revision") != context.summary["detail_revision"]
        or any(type(value) is not int or value < 0 for value in counts.values())
        or sum(cast(int, value) for value in counts.values()) != total
        or summary_counts["retained_detail"] != total
        or not detail_reasons.issubset(_DETAIL_TRUNCATION_REASONS)
        or not topology_reasons.issubset(_TOPOLOGY_TRUNCATION_REASONS)
        or expected_summary_reasons != set(context.summary["detail"]["truncation_reasons"])
        or (
            not last_observed_terminal_detail
            and any(
                counts[name] > summary_counts[name.lower()] for name in _DETAIL_COUNT_FIELD_BY_STATE
            )
        )
        or (
            context.complete
            and any(
                counts[name] != summary_counts[name.lower()]
                for name in _DETAIL_COUNT_FIELD_BY_STATE
            )
        )
    ):
        _raise(WorkflowProgressReadErrorCode.CORRUPT)
    return total if state is None else cast(int, counts[state])


def _related_detail_epoch_record(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return {field: row[f"run_storage__{field}"] for field in _DETAIL_EPOCH_FIELDS} | {
            _DETAIL_TOPOLOGY_REASONS_KEY: row[f"run_storage__{_DETAIL_TOPOLOGY_REASONS_FIELD}"]
        }
    except KeyError as error:
        _raise(WorkflowProgressReadErrorCode.CORRUPT, error)


def _detail_payload_rows(queryset: Any, keys: list[str]) -> list[dict[str, Any]]:
    return list(
        queryset.filter(node_key__in=keys)
        .annotate(_payload_octets=_BlobOctetLength("payload"))
        .annotate(
            _bounded_payload=Case(
                When(
                    _payload_octets__lte=WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES,
                    then=F("payload"),
                ),
                default=Value(None),
                output_field=BinaryField(),
            )
        )
        .order_by("node_key")
        .values(
            "node_key",
            "node_id",
            "invocation_id",
            "state",
            "event_count",
            "truncated",
            "digest",
            "encoded_bytes",
            "decoded_bytes",
            "last_topology_version",
            "last_detail_revision",
            "_payload_octets",
            "_bounded_payload",
            *(f"run_storage__{field}" for field in _DETAIL_EPOCH_FIELDS),
            f"run_storage__{_DETAIL_TOPOLOGY_REASONS_FIELD}",
        )
    )


def list_workflow_node_details(
    execution: Any,
    *,
    authorize: Callable[[RayTaskExecution], bool],
    attempt_number: int | None = None,
    state: str | None = None,
    cursor: str | None = None,
    limit: int = WORKFLOW_PROGRESS_READ_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return one bounded keyset page of normalized latest-state detail."""
    observed_at = datetime.now(UTC)
    using = _database_for(execution)
    with transaction.atomic(using=using):
        context = _locked_context(
            execution,
            authorize=authorize,
            attempt_number=attempt_number,
            generated_at=observed_at,
        )
        applied = _applied_limit(limit)
        normalized_state = _normalized_state(state)
        filters = [["state", normalized_state]] if normalized_state is not None else []
        if not _cursor_length_is_valid(cursor):
            _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
        decoded_cursor, expired = _decode_cursor(
            cursor,
            context=context,
            collection="node_details",
            filters=filters,
            order="stable_node_key_asc",
            limit=applied,
        )
        if expired:
            if decoded_cursor is None:
                _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
            return _expired_page(
                context,
                decoded_cursor,
                collection="node_details",
                generated_at=observed_at,
            )
        if (
            decoded_cursor is not None
            and context.availability == WorkflowProgressDetailAvailability.EXPIRED.value
        ):
            return _expired_page(
                context,
                decoded_cursor,
                collection="node_details",
                generated_at=observed_at,
            )
        if not _ensure_readable(context):
            return _empty_page(
                context,
                collection="node_details",
                generated_at=observed_at,
            )
        if (
            context.summary is None
            or context.summary["topology_version"] is None
            or context.summary["detail_revision"] is None
        ):
            _raise(WorkflowProgressReadErrorCode.MISSING)
        summary = context.summary
        identity = context.identity
        after = None
        prior_seen = 0
        if decoded_cursor is not None:
            if decoded_cursor["position"] is not None:
                _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
            after = decoded_cursor["after"]
            if not _is_lower_hex(after, length=64):
                _raise(WorkflowProgressReadErrorCode.INVALID_CURSOR)
            prior_seen = decoded_cursor["seen"]
        query = _detail_epoch_query(context, using=using)
        if normalized_state is not None:
            query = query.filter(state=normalized_state)
        if after is not None:
            query = query.filter(node_key__gt=after)
        metadata = list(
            query.order_by("node_key").values(
                "node_key",
                "encoded_bytes",
                "decoded_bytes",
                *(f"run_storage__{field}" for field in _DETAIL_EPOCH_FIELDS),
                f"run_storage__{_DETAIL_TOPOLOGY_REASONS_FIELD}",
            )[: applied + 1]
        )
        epoch_record = (
            _related_detail_epoch_record(metadata[0])
            if metadata
            else _detail_epoch_record(context, using=using)
        )
        if epoch_record is None:
            _raise(WorkflowProgressReadErrorCode.MISSING)
        expected_count = _expected_detail_count(
            context,
            epoch_record,
            state=normalized_state,
        )
        if prior_seen > expected_count:
            _raise(WorkflowProgressReadErrorCode.CURSOR_MISMATCH)
        if not metadata and prior_seen < expected_count:
            _raise(WorkflowProgressReadErrorCode.MISSING)
        selected_keys: list[str] = []
        decoded_bytes = 0
        conservative_wire_bytes = 0
        for row in metadata[:applied]:
            encoded = row["encoded_bytes"]
            decoded = row["decoded_bytes"]
            key = row["node_key"]
            if (
                type(encoded) is not int
                or type(decoded) is not int
                or not 1 <= encoded <= WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES
                or decoded != encoded
                or not isinstance(key, str)
                or len(key) != 64
            ):
                _raise(WorkflowProgressReadErrorCode.CORRUPT)
            if selected_keys and (
                decoded_bytes + decoded > WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES
                or conservative_wire_bytes + encoded
                > WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES - 32 * 1024
            ):
                break
            selected_keys.append(key)
            decoded_bytes += decoded
            conservative_wire_bytes += encoded
        rows = _detail_payload_rows(query, selected_keys) if selected_keys else []
        if len(rows) != len(selected_keys):
            _raise(WorkflowProgressReadErrorCode.MISSING)
        items: list[dict[str, Any]] = []
        try:
            for row in rows:
                value = verify_workflow_progress_node_detail_record(
                    row,
                    identity=identity,
                    maximum_topology_version=summary["topology_version"],
                    maximum_detail_revision=summary["detail_revision"],
                )
                items.append(_public_node_detail(value))
        except WorkflowProgressStorageIntegrityError as error:
            _raise(WorkflowProgressReadErrorCode.CORRUPT, error)
        more_metadata = len(metadata) > len(selected_keys)
        base = {
            **_common_envelope(
                context,
                schema="workflow-progress-page",
                generated_at=observed_at,
            ),
            "collection": "node_details",
        }

        def cursor_for_count(count: int) -> str | None:
            emitted = prior_seen + count
            if count == len(items) and not more_metadata:
                if emitted < expected_count:
                    _raise(WorkflowProgressReadErrorCode.MISSING)
                if emitted > expected_count:
                    _raise(WorkflowProgressReadErrorCode.CORRUPT)
                return None
            if count <= 0:
                _raise(WorkflowProgressReadErrorCode.CORRUPT)
            if emitted >= expected_count:
                _raise(WorkflowProgressReadErrorCode.CORRUPT)
            payload = _cursor_payload(
                context,
                collection="node_details",
                filters=filters,
                order="stable_node_key_asc",
                after=selected_keys[count - 1],
                position=None,
                limit=applied,
                seen=emitted,
            )
            return _encode_cursor(payload)

        return _bounded_response(base, items=items, cursor_for_count=cursor_for_count)


def get_workflow_node_detail(
    execution: Any,
    node_id: str,
    *,
    authorize: Callable[[RayTaskExecution], bool],
    attempt_number: int | None = None,
) -> dict[str, Any]:
    """Return one normalized latest-state node record through its indexed key."""
    observed_at = datetime.now(UTC)
    using = _database_for(execution)
    with transaction.atomic(using=using):
        context = _locked_context(
            execution,
            authorize=authorize,
            attempt_number=attempt_number,
            generated_at=observed_at,
        )
        if not isinstance(node_id, str) or not node_id or len(node_id) > 256:
            _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
        try:
            node_bytes = node_id.encode("utf-8")
        except UnicodeEncodeError as error:
            _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT, error)
        if len(node_bytes) > 512:
            _raise(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
        base = _common_envelope(
            context,
            schema="workflow-progress-node",
            generated_at=observed_at,
        )
        if not _ensure_readable(context):
            return {**base, "found": False, "item": None}
        if (
            context.summary is None
            or context.identity is None
            or context.summary["topology_version"] is None
            or context.summary["detail_revision"] is None
        ):
            _raise(WorkflowProgressReadErrorCode.MISSING)
        summary = context.summary
        identity = context.identity
        node_key = hashlib.sha256(node_bytes).hexdigest()
        query = _detail_epoch_query(context, using=using).filter(node_key=node_key)
        rows = _detail_payload_rows(query, [node_key])
        if not rows:
            epoch_record = _detail_epoch_record(context, using=using)
            if epoch_record is None:
                _raise(WorkflowProgressReadErrorCode.MISSING)
            _expected_detail_count(context, epoch_record, state=None)
            return {**base, "found": False, "item": None}
        if len(rows) != 1:
            _raise(WorkflowProgressReadErrorCode.CORRUPT)
        if (
            _expected_detail_count(
                context,
                _related_detail_epoch_record(rows[0]),
                state=None,
            )
            <= 0
        ):
            _raise(WorkflowProgressReadErrorCode.CORRUPT)
        try:
            value = verify_workflow_progress_node_detail_record(
                rows[0],
                identity=identity,
                maximum_topology_version=summary["topology_version"],
                maximum_detail_revision=summary["detail_revision"],
            )
        except WorkflowProgressStorageIntegrityError as error:
            _raise(WorkflowProgressReadErrorCode.CORRUPT, error)
        if value.get("node_id") != node_id:
            _raise(WorkflowProgressReadErrorCode.CORRUPT)
        response = {**base, "found": True, "item": _public_node_detail(value)}
        if len(_wire_bytes(response)) > WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES:
            _raise(WorkflowProgressReadErrorCode.CORRUPT)
        return response


__all__ = [
    "WORKFLOW_PROGRESS_READ_DEFAULT_LIMIT",
    "WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES",
    "WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES",
    "WORKFLOW_PROGRESS_READ_MAX_LIMIT",
    "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES",
    "WorkflowProgressReadError",
    "WorkflowProgressReadErrorCode",
    "get_workflow_node_detail",
    "get_workflow_progress_summary",
    "list_workflow_node_details",
    "list_workflow_topology_edges",
    "list_workflow_topology_nodes",
]
