"""Bounded package-owned storage for workflow topology and latest state.

This module implements storage protocol version 1 from ADR-0004 without
activating schema-v3 publication in the Ray workflow actor.  Every value is
normalized, redacted, and bounded before database persistence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import islice
from secrets import token_hex
from threading import Lock
from typing import Any
from uuid import UUID
from weakref import ReferenceType, ref

from django.db import IntegrityError as DjangoIntegrityError
from django.db import connections, transaction
from django.db.models import BinaryField, Case, Count, F, Func, IntegerField, Q, Sum, Value, When
from django.utils import timezone

from django_ray.conf.settings import get_settings
from django_ray.models import (
    RayTaskExecution,
    TaskState,
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyEncoding,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologyPage,
    WorkflowProgressTopologySlot,
)
from django_ray.redaction import REDACTED, redact_text
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.previews import (
    WorkflowOutputPreviewError,
    _validate_workflow_output_preview,
    read_workflow_output_preview,
    validate_workflow_output_preview,
)
from django_ray.workflow.progress.limits import (
    WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES,
    WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES,
    WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES,
    WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES,
    WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS,
    WORKFLOW_PROGRESS_EVENT_MAX_ENCODED_BYTES,
    WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER,
    WORKFLOW_PROGRESS_LABEL_MAX_BYTES,
    WORKFLOW_PROGRESS_LIMITS_PROFILE,
    WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES,
    WORKFLOW_PROGRESS_METRIC_KEY_MAX_BYTES,
    WORKFLOW_PROGRESS_METRIC_STRING_MAX_BYTES,
    WORKFLOW_PROGRESS_METRICS_MAX_ENCODED_BYTES,
    WORKFLOW_PROGRESS_METRICS_MAX_ITEMS,
    WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
    WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS,
    WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES,
    WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
    WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS,
    WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES,
    WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES,
    WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES,
    WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS,
    WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES,
    WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES,
    WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS,
    WORKFLOW_PROGRESS_VALUE_MAX_DEPTH,
)
from django_ray.workflow.progress.summary import (
    WORKFLOW_PROGRESS_TERMINAL_STATES,
    WorkflowProgressDetailAvailability,
    WorkflowProgressSummaryError,
    WorkflowProgressTruncationReason,
    deserialize_workflow_progress_summary,
    serialize_workflow_progress_summary,
    workflow_progress_detail_is_last_observed,
)

_PAGE_DOMAIN = b"django-ray:workflow-progress:topology-page:v1\x00"
_MANIFEST_DOMAIN = b"django-ray:workflow-progress:topology-manifest:v1\x00"
_DETAIL_DOMAIN = b"django-ray:workflow-progress:node-detail:v1\x00"
_PROTOCOL_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_TOPOLOGY_NODE_KEYS = frozenset(
    {
        "node_id",
        "kind",
        "label",
        "callable_path",
        "runtime_env",
        "ray_options",
    }
)
_TOPOLOGY_EDGE_KEYS = frozenset({"source", "target"})
_TOPOLOGY_PAGE_DESCRIPTOR_KEYS = frozenset(
    {
        "collection",
        "decoded_bytes",
        "digest",
        "encoding",
        "encoded_bytes",
        "item_count",
        "page_index",
    }
)
_INVOCATION_IDENTITY_KEYS = frozenset(
    {
        "schema_version",
        "task_execution_pk",
        "attempt_number",
        "execution_generation",
        "run_id",
        "invocation_id",
    }
)
WORKFLOW_PROGRESS_NODE_DETAIL_SCHEMA_VERSION = 2
_DETAIL_KEYS_V1 = frozenset(
    {
        "schema_version",
        "node_id",
        "invocation_identity",
        "state",
        "progress",
        "execution",
        "fanout",
        "started_at",
        "finished_at",
        "error",
        "recent_events",
    }
)
_DETAIL_KEYS_V2 = _DETAIL_KEYS_V1 | {"output_preview"}
_STORED_DETAIL_KEYS_V1 = _DETAIL_KEYS_V1 | {"truncated"}
_STORED_DETAIL_KEYS_V2 = _DETAIL_KEYS_V2 | {"truncated"}
_PROGRESS_KEYS = frozenset({"current", "total", "percent", "message", "metrics", "updated_at"})
_EXECUTION_KEYS = frozenset(
    {
        "ray_task_id",
        "ray_job_id",
        "ray_node_id",
        "ray_worker_id",
        "assigned_resources",
    }
)
_FANOUT_KEYS = frozenset(
    {
        "max_concurrency",
        "max_items",
        "submitted_items",
        "completed_items",
        "in_flight_items",
        "input_exhausted",
    }
)
_EVENT_KEYS = frozenset({"event", "state", "label", "timestamp"})
_NODE_STATES = frozenset({"PENDING", "RUNNING", "SUCCEEDED", "FAILED"})
_DETAIL_STATE_AGGREGATE_FIELDS = {
    "PENDING": "detail_pending_count",
    "RUNNING": "detail_running_count",
    "SUCCEEDED": "detail_succeeded_count",
    "FAILED": "detail_failed_count",
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
_TOPOLOGY_NODE_KINDS = frozenset({"task", "map"})
_OMITTED_OVERSIZED = "<omitted:oversized>"


class WorkflowProgressStorageError(ValueError):
    """Raised before invalid workflow progress can enter durable storage."""


class WorkflowProgressStorageLimitError(WorkflowProgressStorageError):
    """Raised when one indivisible record exceeds a protocol-v1 limit."""


class WorkflowProgressStorageConflictError(RuntimeError):
    """Raised when a valid publication conflicts with accepted run state."""


class WorkflowProgressStorageIntegrityError(RuntimeError):
    """Raised when stored topology or detail fails bounded verification."""


class WorkflowProgressTopologyCollection(StrEnum):
    """Collection stored in one immutable topology page."""

    NODE = "NODE"
    EDGE = "EDGE"


@dataclass(frozen=True)
class PreparedWorkflowProgressTopologyPage:
    """One canonical immutable topology page prepared outside a transaction."""

    collection: WorkflowProgressTopologyCollection
    page_index: int
    payload: bytes
    digest: str
    item_count: int
    encoded_bytes: int
    decoded_bytes: int


@dataclass(frozen=True)
class PreparedWorkflowProgressTopology:
    """One bounded topology candidate and the evidence needed to verify it."""

    identity: WorkflowRunIdentity
    topology_version: int
    manifest_payload: bytes
    manifest_digest: str
    pages: tuple[PreparedWorkflowProgressTopologyPage, ...]
    node_ids: frozenset[str]
    observed_node_ids: frozenset[str]
    node_kinds: tuple[tuple[str, str], ...]
    edges: tuple[tuple[str, str], ...]
    observed_node_count: int
    observed_edge_count: int
    retained_node_count: int
    retained_edge_count: int
    encoded_bytes: int
    decoded_bytes: int
    truncation_reasons: tuple[str, ...]
    map_node_ids: frozenset[str] = frozenset()
    _capability_token: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class PreparedWorkflowProgressNodeDetail:
    """One canonical normalized latest-state node record."""

    node_id: str
    node_key: str
    state: str
    invocation_id: str | None
    payload: bytes
    digest: str
    encoded_bytes: int
    decoded_bytes: int
    event_count: int
    truncated: bool


@dataclass(frozen=True)
class PreparedWorkflowProgressDetail:
    """A deterministic retained subset suitable for an initial publication."""

    records: tuple[PreparedWorkflowProgressNodeDetail, ...]
    observed_count: int
    encoded_bytes: int
    decoded_bytes: int
    truncation_reasons: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedWorkflowProgressTopology:
    """Bounded verified view of one stored immutable topology manifest."""

    manifest_id: str
    run_storage_id: int
    topology_version: int
    slot: str
    node_ids: frozenset[str]
    node_kinds: tuple[tuple[str, str], ...]
    edges: tuple[tuple[str, str], ...]
    node_count: int
    edge_count: int
    encoded_bytes: int
    decoded_bytes: int
    truncation_reasons: tuple[str, ...]
    map_node_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class VerifiedWorkflowProgressTopologyManifestRecord:
    """Bounded manifest metadata verified without reading linked page payloads."""

    identity: WorkflowRunIdentity
    topology_version: int
    slot: str
    node_count: int
    edge_count: int
    page_descriptors: tuple[dict[str, Any], ...]
    page_encoded_bytes: int
    page_decoded_bytes: int
    expected_link_count: int
    truncation_reasons: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowProgressPublicationResult:
    """Outcome of one exact-fenced atomic storage publication."""

    accepted: bool
    summary: dict[str, Any] | None = None
    changed_node_count: int = 0
    removed_node_count: int = 0


@dataclass(frozen=True)
class WorkflowProgressDetailAuditResult:
    """Verified aggregate evidence for one exact workflow run."""

    run_storage_id: int
    topology_version: int | None
    detail_revision: int | None
    node_count: int
    encoded_bytes: int
    decoded_bytes: int
    event_count: int
    truncated_count: int
    state_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class _PreparedTopologyCapability:
    reference: ReferenceType[PreparedWorkflowProgressTopology]
    signature: tuple[Any, ...]
    evidence_references: tuple[object, ...]
    page_signatures: tuple[tuple[Any, ...], ...]
    trusts_observed_node_ids: bool


_PREPARED_TOPOLOGY_CAPABILITIES: dict[str, _PreparedTopologyCapability] = {}
_PREPARED_TOPOLOGY_CAPABILITIES_LOCK = Lock()


def _type_exact_capability_value(value: Any) -> tuple[type[Any], Any]:
    """Bind scalar evidence without Python's cross-type equality coercions."""
    return type(value), value


def _prepared_topology_capability_signature(
    topology: PreparedWorkflowProgressTopology,
) -> tuple[Any, ...]:
    return (
        _type_exact_capability_value(topology.identity.task_execution_pk),
        _type_exact_capability_value(topology.identity.attempt_number),
        _type_exact_capability_value(topology.identity.execution_generation),
        _type_exact_capability_value(topology.identity.run_id),
        _type_exact_capability_value(topology.topology_version),
        _type_exact_capability_value(topology.manifest_digest),
        _type_exact_capability_value(topology.observed_node_count),
        _type_exact_capability_value(topology.observed_edge_count),
        _type_exact_capability_value(topology.retained_node_count),
        _type_exact_capability_value(topology.retained_edge_count),
        _type_exact_capability_value(topology.encoded_bytes),
        _type_exact_capability_value(topology.decoded_bytes),
    )


def _prepared_topology_capability_evidence_references(
    topology: PreparedWorkflowProgressTopology,
) -> tuple[object, ...]:
    """Retain every identity-bound immutable value while the capability is live.

    Holding the old values prevents CPython from reusing their ``id()`` after a
    hostile ``object.__setattr__`` replacement. Page objects and payloads are
    retained separately so replacing a field of a frozen page is also detected.
    The weak topology reference still controls registry lifetime.
    """
    references: list[object] = [
        topology.manifest_payload,
        topology.pages,
        topology.node_ids,
        topology.observed_node_ids,
        topology.node_kinds,
        topology.edges,
        topology.truncation_reasons,
        topology.map_node_ids,
    ]
    for page in topology.pages:
        references.extend((page, page.payload))
    return tuple(references)


def _prepared_topology_page_signatures(
    topology: PreparedWorkflowProgressTopology,
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            _type_exact_capability_value(page.collection),
            _type_exact_capability_value(page.page_index),
            _type_exact_capability_value(page.digest),
            _type_exact_capability_value(page.item_count),
            _type_exact_capability_value(page.encoded_bytes),
            _type_exact_capability_value(page.decoded_bytes),
        )
        for page in topology.pages
    )


def _prepared_topology_capability_record_matches(
    capability: _PreparedTopologyCapability,
    topology: PreparedWorkflowProgressTopology,
) -> bool:
    try:
        current_signature = _prepared_topology_capability_signature(topology)
        current_references = _prepared_topology_capability_evidence_references(topology)
        current_page_signatures = _prepared_topology_page_signatures(topology)
    except (AttributeError, TypeError):
        return False
    return bool(
        capability.reference() is topology
        and capability.signature == current_signature
        and capability.page_signatures == current_page_signatures
        and len(capability.evidence_references) == len(current_references)
        and all(
            retained is current
            for retained, current in zip(
                capability.evidence_references,
                current_references,
                strict=True,
            )
        )
    )


def _drop_prepared_topology_capability(
    token: str,
    observed: ReferenceType[PreparedWorkflowProgressTopology],
) -> None:
    with _PREPARED_TOPOLOGY_CAPABILITIES_LOCK:
        current = _PREPARED_TOPOLOGY_CAPABILITIES.get(token)
        if current is not None and current.reference is observed:
            _PREPARED_TOPOLOGY_CAPABILITIES.pop(token, None)


def _register_prepared_topology_capability(
    topology: PreparedWorkflowProgressTopology,
    *,
    trust_observed_node_ids: bool = False,
) -> None:
    with _PREPARED_TOPOLOGY_CAPABILITIES_LOCK:
        token = topology._capability_token
        current = _PREPARED_TOPOLOGY_CAPABILITIES.get(token or "")
        signature = _prepared_topology_capability_signature(topology)
        retained_observed_trust = bool(
            current is not None
            and current.trusts_observed_node_ids
            and _prepared_topology_capability_record_matches(current, topology)
        )
        if token is None or (current is not None and current.reference() is not topology):
            token = token_hex(32)
            object.__setattr__(topology, "_capability_token", token)
        observed = ref(
            topology,
            lambda reference, capability_token=token: _drop_prepared_topology_capability(
                capability_token,
                reference,
            ),
        )
        _PREPARED_TOPOLOGY_CAPABILITIES[token] = _PreparedTopologyCapability(
            reference=observed,
            signature=signature,
            evidence_references=_prepared_topology_capability_evidence_references(topology),
            page_signatures=_prepared_topology_page_signatures(topology),
            trusts_observed_node_ids=trust_observed_node_ids or retained_observed_trust,
        )


def _prepared_topology_capability_matches(
    topology: PreparedWorkflowProgressTopology,
) -> bool:
    token = topology._capability_token
    if token is None:
        return False
    with _PREPARED_TOPOLOGY_CAPABILITIES_LOCK:
        capability = _PREPARED_TOPOLOGY_CAPABILITIES.get(token)
        return bool(
            capability is not None
            and _prepared_topology_capability_record_matches(capability, topology)
        )


def _prepared_topology_observed_membership_capability_matches(
    topology: PreparedWorkflowProgressTopology,
) -> bool:
    """Return whether this exact object owns complete observed membership trust."""
    token = topology._capability_token
    if token is None:
        return False
    with _PREPARED_TOPOLOGY_CAPABILITIES_LOCK:
        capability = _PREPARED_TOPOLOGY_CAPABILITIES.get(token)
        return bool(
            capability is not None
            and capability.trusts_observed_node_ids
            and _prepared_topology_capability_record_matches(capability, topology)
        )


def _reset_prepared_topology_capabilities_after_fork() -> None:
    """Drop process-local authority and inherited lock state in a fork child."""
    global _PREPARED_TOPOLOGY_CAPABILITIES
    global _PREPARED_TOPOLOGY_CAPABILITIES_LOCK
    _PREPARED_TOPOLOGY_CAPABILITIES = {}
    _PREPARED_TOPOLOGY_CAPABILITIES_LOCK = Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_prepared_topology_capabilities_after_fork)


@dataclass
class _MetadataBudget:
    remaining_bytes: int = WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES

    def consume(self, amount: int, name: str) -> None:
        self.remaining_bytes -= amount
        if self.remaining_bytes < 0:
            raise WorkflowProgressStorageLimitError(f"{name} exceeds the record byte budget")


class _BlobOctetLength(Func):
    function = "OCTET_LENGTH"
    output_field = IntegerField()

    def as_sqlite(self, compiler, connection, **extra_context):
        return self.as_sql(
            compiler,
            connection,
            function="LENGTH",
            **extra_context,
        )

    def as_oracle(self, compiler, connection, **extra_context):
        return self.as_sql(
            compiler,
            connection,
            function="LENGTHB",
            **extra_context,
        )


def _encode_truncation_reasons(values: Iterable[str]) -> str:
    valid = {reason.value for reason in WorkflowProgressTruncationReason}
    supplied = tuple(values)
    if any(not isinstance(reason, str) or reason not in valid for reason in supplied):
        raise WorkflowProgressStorageError("workflow truncation reasons are invalid")
    reasons = tuple(sorted(set(supplied)))
    encoded = ",".join(reasons)
    if len(encoded) > 256:
        raise WorkflowProgressStorageError("workflow truncation reasons exceed storage bounds")
    return encoded


def _decode_truncation_reasons(value: Any, *, stored: bool) -> tuple[str, ...]:
    error_type = WorkflowProgressStorageIntegrityError if stored else WorkflowProgressStorageError
    if not isinstance(value, str) or len(value) > 256:
        raise error_type("workflow truncation reasons are invalid")
    reasons = tuple(value.split(",")) if value else ()
    try:
        canonical = _encode_truncation_reasons(reasons)
    except WorkflowProgressStorageError as error:
        raise error_type("workflow truncation reasons are invalid") from error
    if canonical != value:
        raise error_type("workflow truncation reasons are not canonical")
    return reasons


def _as_bytes(value: Any, name: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    raise WorkflowProgressStorageIntegrityError(f"{name} is not binary data")


def _decode_canonical_payload(payload: bytes, name: str) -> Any:
    try:
        value = json.loads(payload)
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise WorkflowProgressStorageIntegrityError(f"{name} is not valid JSON") from error
    try:
        canonical = _canonical_json_bytes(value)
    except WorkflowProgressStorageError as error:
        raise WorkflowProgressStorageIntegrityError(f"{name} is not canonical JSON") from error
    if canonical != payload:
        raise WorkflowProgressStorageIntegrityError(f"{name} is not canonical JSON")
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, UnicodeEncodeError, ValueError) as error:
        raise WorkflowProgressStorageError(
            "workflow progress record is not canonical JSON"
        ) from error


def _digest(domain: bytes, payload: bytes) -> str:
    return hashlib.sha256(domain + payload).hexdigest()


def _utf8_bytes(value: Any, name: str) -> bytes:
    if not isinstance(value, str):
        raise WorkflowProgressStorageError(f"{name} must be text")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise WorkflowProgressStorageError(f"{name} must contain valid UTF-8") from error


def _bounded_text(
    value: Any,
    name: str,
    *,
    max_bytes: int,
    nullable: bool = False,
    redact: bool = False,
) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise WorkflowProgressStorageError(f"{name} must be text")
    normalized = redact_text(value) if redact else value
    encoded = _utf8_bytes(normalized, name)
    if not encoded or len(encoded) > max_bytes:
        raise WorkflowProgressStorageLimitError(
            f"{name} must contain between 1 and {max_bytes} UTF-8 bytes"
        )
    return normalized


def _bounded_identity_text(
    value: Any,
    name: str,
    *,
    max_bytes: int,
    nullable: bool = False,
    enforce_current_policy: bool = True,
) -> str | None:
    """Validate an identity without allowing redaction to create collisions."""
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise WorkflowProgressStorageError(f"{name} must be text")
    encoded = _utf8_bytes(value, name)
    if not encoded:
        raise WorkflowProgressStorageError(f"{name} cannot be empty")
    if len(encoded) > max_bytes:
        raise WorkflowProgressStorageLimitError(f"{name} exceeds {max_bytes} UTF-8 bytes")
    if enforce_current_policy:
        normalized = redact_text(value)
        if normalized == REDACTED:
            raise WorkflowProgressStorageError(f"{name} resembles sensitive data")
        if normalized != value:
            raise WorkflowProgressStorageError(f"{name} contains unsafe characters")
    return value


def _bounded_identity_characters(
    value: Any,
    name: str,
    *,
    max_characters: int,
    enforce_current_policy: bool = True,
) -> str:
    if not isinstance(value, str):
        raise WorkflowProgressStorageError(f"{name} must be text")
    _utf8_bytes(value, name)
    if not value or len(value) > max_characters:
        raise WorkflowProgressStorageLimitError(
            f"{name} must contain between 1 and {max_characters} characters"
        )
    if enforce_current_policy:
        normalized = redact_text(value)
        if normalized == REDACTED:
            raise WorkflowProgressStorageError(f"{name} resembles sensitive data")
        if normalized != value:
            raise WorkflowProgressStorageError(f"{name} contains unsafe characters")
    return value


def _bounded_redacted_text(
    value: Any,
    name: str,
    *,
    max_bytes: int,
    nullable: bool = False,
    apply_current_policy: bool = True,
) -> tuple[str | None, bool]:
    if nullable and value is None:
        return None, False
    if not isinstance(value, str):
        raise WorkflowProgressStorageError(f"{name} must be text")
    encoded = _utf8_bytes(value, name)
    if len(encoded) > max_bytes:
        return _OMITTED_OVERSIZED, True
    normalized = redact_text(value) if apply_current_policy else value
    if len(_utf8_bytes(normalized, name)) <= max_bytes:
        return normalized, False
    return _OMITTED_OVERSIZED, True


def _exact_mapping(value: Any, keys: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WorkflowProgressStorageError(f"{name} must contain the exact protocol fields")
    return value


def _validate_run_identity(identity: WorkflowRunIdentity) -> None:
    if not isinstance(identity, WorkflowRunIdentity):
        raise WorkflowProgressStorageError("identity must be a WorkflowRunIdentity")
    for name, minimum in (
        ("task_execution_pk", 1),
        ("attempt_number", 1),
        ("execution_generation", 0),
    ):
        value = getattr(identity, name)
        if (
            type(value) is not int
            or value < minimum
            or value > WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER
        ):
            raise WorkflowProgressStorageError(f"identity.{name} is outside the durable range")
    if not isinstance(identity.run_id, str):
        raise WorkflowProgressStorageError("identity.run_id must be a canonical UUID")
    try:
        if str(UUID(identity.run_id)) != identity.run_id:
            raise WorkflowProgressStorageError("identity.run_id must be a canonical UUID")
    except (AttributeError, ValueError) as error:
        raise WorkflowProgressStorageError("identity.run_id must be a canonical UUID") from error


def _finite_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise WorkflowProgressStorageError(f"{name} must be a finite number")
    try:
        normalized = float(value)
    except OverflowError as error:
        raise WorkflowProgressStorageError(f"{name} must be a finite number") from error
    if not math.isfinite(normalized) or (minimum is not None and normalized < minimum):
        raise WorkflowProgressStorageError(f"{name} must be a finite number")
    return normalized


def _bounded_int(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER,
    nullable: bool = False,
) -> int | None:
    if nullable and value is None:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise WorkflowProgressStorageError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _normalize_metadata(
    value: Any,
    name: str,
    *,
    depth: int = 0,
    budget: _MetadataBudget | None = None,
    apply_current_policy: bool = True,
) -> tuple[Any, bool]:
    """Return bounded JSON metadata with redaction applied before digesting."""
    if budget is None:
        budget = _MetadataBudget()
    if depth > WORKFLOW_PROGRESS_VALUE_MAX_DEPTH:
        raise WorkflowProgressStorageLimitError(f"{name} exceeds the nesting limit")
    if value is None:
        budget.consume(4, name)
        return value, False
    if isinstance(value, bool):
        budget.consume(5, name)
        return value, False
    if isinstance(value, int):
        if not -(1 << 63) <= value <= WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER:
            raise WorkflowProgressStorageError(f"{name} integer is outside the durable range")
        budget.consume(24, name)
        return value, False
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowProgressStorageError(f"{name} must contain finite numbers")
        budget.consume(24, name)
        return value, False
    if isinstance(value, str):
        encoded = _utf8_bytes(value, name)
        if len(encoded) > WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES:
            budget.consume(len(_OMITTED_OVERSIZED) + 2, name)
            return _OMITTED_OVERSIZED, True
        budget.consume(len(encoded) + 2, name)
        return (redact_text(value) if apply_current_policy else value), False
    if isinstance(value, Mapping):
        budget.consume(2, name)
        normalized: dict[str, Any] = {}
        truncated = False
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkflowProgressStorageError(f"{name} object keys must be text")
            key_bytes = _utf8_bytes(key, f"{name} key")
            if not key_bytes or len(key_bytes) > WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES:
                raise WorkflowProgressStorageLimitError(f"{name} contains an oversized key")
            budget.consume(len(key_bytes) + 4, name)
            normalized_key = redact_text(key) if apply_current_policy else key
            if normalized_key == REDACTED:
                truncated = True
                continue
            if not normalized_key:
                raise WorkflowProgressStorageError(f"{name} contains an empty normalized key")
            if normalized_key in normalized:
                raise WorkflowProgressStorageError(f"{name} contains a duplicate normalized key")
            normalized_item, item_truncated = _normalize_metadata(
                item,
                f"{name}.{normalized_key}",
                depth=depth + 1,
                budget=budget,
                apply_current_policy=apply_current_policy,
            )
            normalized[normalized_key] = normalized_item
            truncated = truncated or item_truncated
        return normalized, truncated
    if isinstance(value, list | tuple):
        budget.consume(2, name)
        normalized_items: list[Any] = []
        truncated = False
        for index, item in enumerate(value):
            budget.consume(1, name)
            normalized_item, item_truncated = _normalize_metadata(
                item,
                f"{name}[{index}]",
                depth=depth + 1,
                budget=budget,
                apply_current_policy=apply_current_policy,
            )
            normalized_items.append(normalized_item)
            truncated = truncated or item_truncated
        return normalized_items, truncated
    raise WorkflowProgressStorageError(f"{name} must contain only JSON values")


def _normalize_metadata_object(
    value: Any,
    name: str,
    *,
    apply_current_policy: bool = True,
) -> tuple[dict[str, Any], bool]:
    normalized, truncated = _normalize_metadata(
        value,
        name,
        apply_current_policy=apply_current_policy,
    )
    if not isinstance(normalized, dict):
        raise WorkflowProgressStorageError(f"{name} must be an object")
    return normalized, truncated


def _timestamp(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        normalized = _finite_number(value, name, minimum=0.0)
        try:
            parsed = datetime.fromtimestamp(normalized, tz=UTC)
        except (OverflowError, OSError, ValueError) as error:
            raise WorkflowProgressStorageError(f"{name} is outside the timestamp range") from error
        return parsed.isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 32:
        raise WorkflowProgressStorageError(f"{name} must be a bounded UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(UTC)
    except ValueError as error:
        raise WorkflowProgressStorageError(f"{name} must be a bounded UTC timestamp") from error
    canonical = parsed.isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise WorkflowProgressStorageError(f"{name} must use canonical UTC encoding")
    return canonical


def _identifier(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or _PROTOCOL_IDENTIFIER.fullmatch(value) is None:
        raise WorkflowProgressStorageError(f"{name} must be a bounded protocol identifier")
    return value


def _normalize_invocation_identity(
    value: Any,
    *,
    identity: WorkflowRunIdentity,
    enforce_current_policy: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return None, None
    invocation = _exact_mapping(
        value,
        _INVOCATION_IDENTITY_KEYS,
        "invocation_identity",
    )
    if invocation["schema_version"] != 1 or type(invocation["schema_version"]) is not int:
        raise WorkflowProgressStorageError("invocation_identity schema_version is unsupported")
    expected = identity.as_dict()
    for name in (
        "task_execution_pk",
        "attempt_number",
        "execution_generation",
        "run_id",
    ):
        if invocation[name] != expected[name] or type(invocation[name]) is not type(expected[name]):
            raise WorkflowProgressStorageError(
                "invocation_identity must match the complete workflow run identity"
            )
    invocation_id = _bounded_identity_characters(
        invocation["invocation_id"],
        "invocation_identity.invocation_id",
        max_characters=128,
        enforce_current_policy=enforce_current_policy,
    )
    return {
        "attempt_number": identity.attempt_number,
        "execution_generation": identity.execution_generation,
        "invocation_id": invocation_id,
        "run_id": identity.run_id,
        "schema_version": 1,
        "task_execution_pk": identity.task_execution_pk,
    }, invocation_id


def _metrics(
    value: Any,
    name: str,
    *,
    apply_current_policy: bool = True,
) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, Mapping) or len(value) > WORKFLOW_PROGRESS_METRICS_MAX_ITEMS:
        raise WorkflowProgressStorageLimitError(f"{name} exceeds the metrics item limit")
    normalized: dict[str, Any] = {}
    truncated = False
    keys = list(value)
    if any(not isinstance(key, str) for key in keys):
        raise WorkflowProgressStorageError(f"{name} keys must be text")
    for key in sorted(keys):
        key_bytes = _utf8_bytes(key, f"{name} key")
        if not key_bytes or len(key_bytes) > WORKFLOW_PROGRESS_METRIC_KEY_MAX_BYTES:
            raise WorkflowProgressStorageLimitError(f"{name} contains an oversized key")
        normalized_key = redact_text(key) if apply_current_policy else key
        if normalized_key == REDACTED:
            truncated = True
            continue
        if not normalized_key:
            raise WorkflowProgressStorageError(f"{name} contains an empty normalized key")
        if normalized_key in normalized:
            raise WorkflowProgressStorageError(f"{name} contains a duplicate normalized key")
        item = value[key]
        if isinstance(item, float) and not math.isfinite(item):
            raise WorkflowProgressStorageError(f"{name}.{normalized_key} must be finite")
        if item is None or isinstance(item, (bool, int, float)):
            normalized[normalized_key] = item
        elif isinstance(item, str):
            encoded = _utf8_bytes(item, f"{name}.{normalized_key}")
            if len(encoded) > WORKFLOW_PROGRESS_METRIC_STRING_MAX_BYTES:
                normalized[normalized_key] = _OMITTED_OVERSIZED
                truncated = True
            else:
                normalized[normalized_key] = redact_text(item) if apply_current_policy else item
        else:
            raise WorkflowProgressStorageError(f"{name}.{normalized_key} must be a scalar")
    encoded = _canonical_json_bytes(normalized)
    if len(encoded) <= WORKFLOW_PROGRESS_METRICS_MAX_ENCODED_BYTES:
        return normalized, truncated
    marker = {"_omitted": f"sha256:{hashlib.sha256(encoded).hexdigest()}"}
    return marker, True


def _assigned_resources(
    value: Any,
    name: str,
    *,
    apply_current_policy: bool = True,
    allow_policy_omission: bool = False,
) -> tuple[dict[str, float], bool]:
    if not isinstance(value, Mapping) or len(value) > WORKFLOW_PROGRESS_METRICS_MAX_ITEMS:
        raise WorkflowProgressStorageLimitError(f"{name} exceeds the resource item limit")
    normalized: dict[str, float] = {}
    truncated = False
    keys = list(value)
    if any(not isinstance(key, str) for key in keys):
        raise WorkflowProgressStorageError(f"{name} keys must be text")
    for key in sorted(keys):
        key_bytes = _utf8_bytes(key, f"{name} key")
        if not key_bytes or len(key_bytes) > WORKFLOW_PROGRESS_METRIC_KEY_MAX_BYTES:
            raise WorkflowProgressStorageLimitError(f"{name} contains an oversized key")
        normalized_key = redact_text(key) if apply_current_policy else key
        if normalized_key == REDACTED:
            if allow_policy_omission:
                truncated = True
                continue
            raise WorkflowProgressStorageError(f"{name} contains a sensitive-looking key")
        if not normalized_key:
            raise WorkflowProgressStorageError(f"{name} contains an empty normalized key")
        if normalized_key in normalized:
            raise WorkflowProgressStorageError(f"{name} contains a duplicate normalized key")
        normalized[normalized_key] = _finite_number(
            value[key],
            f"{name}.{normalized_key}",
            minimum=0.0,
        )
    if len(_canonical_json_bytes(normalized)) > WORKFLOW_PROGRESS_METRICS_MAX_ENCODED_BYTES:
        raise WorkflowProgressStorageLimitError(f"{name} exceeds its byte limit")
    return normalized, truncated


def _normalize_topology_node(
    value: Any,
    *,
    apply_current_policy: bool = True,
) -> tuple[dict[str, Any], bool]:
    node = _exact_mapping(value, _TOPOLOGY_NODE_KEYS, "topology node")
    node_id = _bounded_identity_text(
        node["node_id"],
        "topology node_id",
        max_bytes=WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
        enforce_current_policy=apply_current_policy,
    )
    kind = _identifier(node["kind"], "topology node kind")
    if kind not in _TOPOLOGY_NODE_KINDS:
        raise WorkflowProgressStorageError("topology node kind is unsupported")
    label, label_truncated = _bounded_redacted_text(
        node["label"],
        "topology node label",
        max_bytes=WORKFLOW_PROGRESS_LABEL_MAX_BYTES,
        apply_current_policy=apply_current_policy,
    )
    callable_path, path_truncated = _bounded_redacted_text(
        node["callable_path"],
        "topology callable_path",
        max_bytes=WORKFLOW_PROGRESS_LABEL_MAX_BYTES,
        nullable=True,
        apply_current_policy=apply_current_policy,
    )
    runtime_env, runtime_env_truncated = _normalize_metadata_object(
        node["runtime_env"],
        "topology runtime_env",
        apply_current_policy=apply_current_policy,
    )
    ray_options, ray_options_truncated = _normalize_metadata_object(
        node["ray_options"],
        "topology ray_options",
        apply_current_policy=apply_current_policy,
    )
    normalized = {
        "callable_path": callable_path,
        "kind": kind,
        "label": label,
        "node_id": node_id,
        "ray_options": ray_options,
        "runtime_env": runtime_env,
    }
    if len(_canonical_json_bytes(normalized)) > WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES:
        raise WorkflowProgressStorageLimitError("topology node exceeds the record byte limit")
    return (
        normalized,
        label_truncated or path_truncated or runtime_env_truncated or ray_options_truncated,
    )


def _normalize_topology_edge(
    value: Any,
    *,
    apply_current_policy: bool = True,
) -> dict[str, str]:
    edge = _exact_mapping(value, _TOPOLOGY_EDGE_KEYS, "topology edge")
    source = _bounded_identity_text(
        edge["source"],
        "topology edge source",
        max_bytes=WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
        enforce_current_policy=apply_current_policy,
    )
    target = _bounded_identity_text(
        edge["target"],
        "topology edge target",
        max_bytes=WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
        enforce_current_policy=apply_current_policy,
    )
    if source is None or target is None:
        raise AssertionError("non-null topology edge identity normalized to None")
    normalized = {"source": source, "target": target}
    if len(_canonical_json_bytes(normalized)) > WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES:
        raise WorkflowProgressStorageLimitError("topology edge exceeds the record byte limit")
    return normalized


def _build_pages(
    collection: WorkflowProgressTopologyCollection,
    records: list[dict[str, Any]],
) -> tuple[list[PreparedWorkflowProgressTopologyPage], int]:
    pages: list[PreparedWorkflowProgressTopologyPage] = []
    consumed = 0
    while consumed < len(records):
        page_records: list[dict[str, Any]] = []
        while consumed + len(page_records) < len(records):
            candidate = records[consumed + len(page_records)]
            trial = {
                "collection": collection.value,
                "records": [*page_records, candidate],
                "schema_version": WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
            }
            encoded = _canonical_json_bytes(trial)
            if page_records and (
                len(page_records) >= WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS
                or len(encoded) > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES
                or len(encoded) > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES
            ):
                break
            if (
                len(encoded) > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES
                or len(encoded) > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES
            ):
                raise WorkflowProgressStorageLimitError(
                    "one topology record cannot fit in a storage page"
                )
            page_records.append(candidate)
            if len(page_records) == WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS:
                break
        payload = _canonical_json_bytes(
            {
                "collection": collection.value,
                "records": page_records,
                "schema_version": WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
            }
        )
        pages.append(
            PreparedWorkflowProgressTopologyPage(
                collection=collection,
                page_index=len(pages),
                payload=payload,
                digest=_digest(_PAGE_DOMAIN, payload),
                item_count=len(page_records),
                encoded_bytes=len(payload),
                decoded_bytes=len(payload),
            )
        )
        consumed += len(page_records)
    return pages, consumed


def _topology_manifest_payload(
    *,
    identity: WorkflowRunIdentity,
    topology_version: int,
    pages: list[PreparedWorkflowProgressTopologyPage],
    node_count: int,
    edge_count: int,
    truncation_reasons: Iterable[str],
) -> bytes:
    page_descriptors = [
        {
            "collection": page.collection.value,
            "decoded_bytes": page.decoded_bytes,
            "digest": page.digest,
            "encoding": "identity",
            "encoded_bytes": page.encoded_bytes,
            "item_count": page.item_count,
            "page_index": page.page_index,
        }
        for page in pages
    ]
    return _canonical_json_bytes(
        {
            "edge_count": edge_count,
            "node_count": node_count,
            "pages": page_descriptors,
            "run_identity": identity.as_dict(),
            "schema_version": WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
            "topology_version": topology_version,
            "truncation_reasons": list(truncation_reasons),
        }
    )


def _prepare_workflow_progress_topology_materialized(
    identity: WorkflowRunIdentity,
    topology_version: int,
    nodes: Iterable[Mapping[str, Any]],
    edges: Iterable[Mapping[str, Any]],
) -> PreparedWorkflowProgressTopology:
    """Normalize and deterministically bound one topology candidate."""
    _validate_run_identity(identity)
    if (
        type(topology_version) is not int
        or topology_version <= 0
        or topology_version > WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER
    ):
        raise WorkflowProgressStorageError(
            "topology_version must be a positive integer within the durable range"
        )

    reasons: set[str] = set()
    normalized_nodes: list[dict[str, Any]] = []
    observed_nodes = 0
    observed_node_ids: set[str] = set()
    for value in nodes:
        observed_nodes += 1
        node = _exact_mapping(value, _TOPOLOGY_NODE_KEYS, "topology node")
        try:
            observed_node_id = _bounded_identity_text(
                node["node_id"],
                "topology node_id",
                max_bytes=WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
            )
        except WorkflowProgressStorageLimitError:
            reasons.add(WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
            continue
        if observed_node_id is None:
            raise AssertionError("non-null topology node identity normalized to None")
        if observed_node_id in observed_node_ids:
            raise WorkflowProgressStorageError("topology contains a duplicate node_id")
        observed_node_ids.add(observed_node_id)
        try:
            normalized, truncated = _normalize_topology_node(value)
        except WorkflowProgressStorageLimitError:
            reasons.add(WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
            continue
        if truncated:
            reasons.add(WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
        normalized_nodes.append(normalized)
    normalized_nodes.sort(key=lambda item: str(item["node_id"]))
    if len(normalized_nodes) > WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS:
        normalized_nodes = normalized_nodes[:WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS]
        reasons.add(WorkflowProgressTruncationReason.NODE_COUNT_LIMIT.value)
    retained_node_ids = {str(item["node_id"]) for item in normalized_nodes}

    normalized_edges: list[dict[str, Any]] = []
    observed_edges = 0
    seen_edges: set[tuple[str, str]] = set()
    for value in edges:
        observed_edges += 1
        try:
            normalized = _normalize_topology_edge(value)
        except WorkflowProgressStorageLimitError:
            reasons.add(WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
            continue
        key = (normalized["source"], normalized["target"])
        if key in seen_edges:
            raise WorkflowProgressStorageError("topology contains a duplicate edge")
        seen_edges.add(key)
        if key[0] not in observed_node_ids or key[1] not in observed_node_ids:
            raise WorkflowProgressStorageError("topology edge references an unknown node_id")
        if key[0] not in retained_node_ids or key[1] not in retained_node_ids:
            continue
        normalized_edges.append(normalized)
    normalized_edges.sort(key=lambda item: (item["source"], item["target"]))
    if len(normalized_edges) > WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS:
        normalized_edges = normalized_edges[:WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS]
        reasons.add(WorkflowProgressTruncationReason.EDGE_COUNT_LIMIT.value)

    node_pages, _ = _build_pages(WorkflowProgressTopologyCollection.NODE, normalized_nodes)
    edge_pages, _ = _build_pages(WorkflowProgressTopologyCollection.EDGE, normalized_edges)
    retained_pages = [*node_pages, *edge_pages]
    retained_nodes = len(normalized_nodes)
    retained_edges = len(normalized_edges)
    while True:
        manifest_payload = _topology_manifest_payload(
            identity=identity,
            topology_version=topology_version,
            pages=retained_pages,
            node_count=retained_nodes,
            edge_count=retained_edges,
            truncation_reasons=sorted(reasons),
        )
        encoded_bytes = len(manifest_payload) + sum(page.encoded_bytes for page in retained_pages)
        decoded_bytes = len(manifest_payload) + sum(page.decoded_bytes for page in retained_pages)
        manifest_fits = (
            len(manifest_payload) <= WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES
        )
        encoded_fits = encoded_bytes <= WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES
        decoded_fits = decoded_bytes <= WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES
        if manifest_fits and encoded_fits and decoded_fits:
            break
        if not encoded_fits or not manifest_fits:
            reasons.add(WorkflowProgressTruncationReason.TOPOLOGY_ENCODED_BYTES.value)
        if not decoded_fits:
            reasons.add(WorkflowProgressTruncationReason.TOPOLOGY_DECODED_BYTES.value)
        if not retained_pages:
            raise WorkflowProgressStorageLimitError(
                "empty topology manifest exceeds the protocol byte limit"
            )
        removed_page = retained_pages.pop()
        if removed_page.collection is WorkflowProgressTopologyCollection.NODE:
            retained_nodes -= removed_page.item_count
        else:
            retained_edges -= removed_page.item_count

    retained_node_records = normalized_nodes[:retained_nodes]
    retained_node_ids = frozenset(str(item["node_id"]) for item in retained_node_records)
    prepared = PreparedWorkflowProgressTopology(
        identity=identity,
        topology_version=topology_version,
        manifest_payload=manifest_payload,
        manifest_digest=_digest(_MANIFEST_DOMAIN, manifest_payload),
        pages=tuple(retained_pages),
        node_ids=retained_node_ids,
        observed_node_ids=frozenset(observed_node_ids),
        node_kinds=tuple(
            (str(item["node_id"]), str(item["kind"])) for item in retained_node_records
        ),
        edges=tuple(
            (str(item["source"]), str(item["target"])) for item in normalized_edges[:retained_edges]
        ),
        observed_node_count=observed_nodes,
        observed_edge_count=observed_edges,
        retained_node_count=retained_nodes,
        retained_edge_count=retained_edges,
        encoded_bytes=encoded_bytes,
        decoded_bytes=decoded_bytes,
        truncation_reasons=tuple(sorted(reasons)),
        map_node_ids=frozenset(
            str(item["node_id"]) for item in retained_node_records if item["kind"] == "map"
        ),
    )
    _register_prepared_topology_capability(
        prepared,
        trust_observed_node_ids=True,
    )
    return prepared


def _normalize_event(
    value: Any,
    *,
    apply_current_policy: bool = True,
) -> tuple[dict[str, Any], bool]:
    event = _exact_mapping(value, _EVENT_KEYS, "recent event")
    label, truncated = _bounded_redacted_text(
        event["label"],
        "recent event label",
        max_bytes=WORKFLOW_PROGRESS_LABEL_MAX_BYTES,
        apply_current_policy=apply_current_policy,
    )
    state = event["state"]
    if not isinstance(state, str) or state not in _NODE_STATES:
        raise WorkflowProgressStorageError("recent event state is unsupported")
    normalized = {
        "event": _identifier(event["event"], "recent event type"),
        "label": label,
        "state": state,
        "timestamp": _timestamp(event["timestamp"], "recent event timestamp"),
    }
    encoded = _canonical_json_bytes(normalized)
    if len(encoded) > WORKFLOW_PROGRESS_EVENT_MAX_ENCODED_BYTES:
        raise WorkflowProgressStorageLimitError("recent event exceeds its byte limit")
    return normalized, truncated


def _event_sort_key(
    event: Mapping[str, Any],
    *,
    node_id: str = "",
    occurrence: int = 0,
) -> tuple[Any, ...]:
    timestamp = event["timestamp"]
    if not isinstance(timestamp, str):
        raise AssertionError("normalized event timestamp must be text")
    parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    return (
        parsed,
        node_id,
        str(event["event"]),
        str(event["state"]),
        str(event["label"]),
        occurrence,
    )


def _normalize_fanout(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    fanout = _exact_mapping(value, _FANOUT_KEYS, "node fanout")
    max_concurrency = _bounded_int(
        fanout["max_concurrency"],
        "node fanout max_concurrency",
        minimum=1,
        nullable=True,
    )
    max_items = _bounded_int(
        fanout["max_items"],
        "node fanout max_items",
        minimum=1,
        nullable=True,
    )
    submitted = _bounded_int(
        fanout["submitted_items"],
        "node fanout submitted_items",
    )
    completed = _bounded_int(
        fanout["completed_items"],
        "node fanout completed_items",
    )
    in_flight = _bounded_int(
        fanout["in_flight_items"],
        "node fanout in_flight_items",
    )
    input_exhausted = fanout["input_exhausted"]
    if type(input_exhausted) is not bool:
        raise WorkflowProgressStorageError("node fanout input_exhausted must be boolean")
    if submitted is None or completed is None or in_flight is None:
        raise AssertionError("non-null fanout counter normalized to None")
    if completed > submitted or in_flight != submitted - completed:
        raise WorkflowProgressStorageError("node fanout counters are inconsistent")
    if max_items is not None and submitted > max_items:
        raise WorkflowProgressStorageError("node fanout exceeds max_items")
    if max_concurrency is not None and in_flight > max_concurrency:
        raise WorkflowProgressStorageError("node fanout exceeds max_concurrency")
    return {
        "completed_items": completed,
        "in_flight_items": in_flight,
        "input_exhausted": input_exhausted,
        "max_concurrency": max_concurrency,
        "max_items": max_items,
        "submitted_items": submitted,
    }


def _prepared_node_detail(
    normalized: dict[str, Any],
    *,
    invocation_id: str | None,
    truncated: bool,
) -> PreparedWorkflowProgressNodeDetail:
    durable = dict(normalized)
    durable["truncated"] = truncated
    payload = _canonical_json_bytes(durable)
    if len(payload) > WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES:
        raise WorkflowProgressStorageLimitError("node detail exceeds the record byte limit")
    node_id = str(normalized["node_id"])
    node_bytes = _utf8_bytes(node_id, "node detail node_id")
    recent_events = normalized["recent_events"]
    if not isinstance(recent_events, list):
        raise AssertionError("normalized recent_events must be a list")
    return PreparedWorkflowProgressNodeDetail(
        node_id=node_id,
        node_key=hashlib.sha256(node_bytes).hexdigest(),
        state=str(normalized["state"]),
        invocation_id=invocation_id,
        payload=payload,
        digest=_digest(_DETAIL_DOMAIN, payload),
        encoded_bytes=len(payload),
        decoded_bytes=len(payload),
        event_count=len(recent_events),
        truncated=truncated,
    )


def _prepare_workflow_progress_node_detail(
    value: Mapping[str, Any],
    *,
    identity: WorkflowRunIdentity,
    allow_stored_truncation: bool,
    enforce_current_preview_redaction: bool = True,
    allow_current_policy_truncation: bool = False,
    apply_current_text_policy: bool = True,
) -> PreparedWorkflowProgressNodeDetail:
    """Normalize a producer record or revalidate its durable truncation evidence."""
    _validate_run_identity(identity)
    if not isinstance(value, Mapping):
        raise WorkflowProgressStorageError("node detail must be a mapping")
    stored_truncated: bool | None = None
    if allow_stored_truncation:
        durable_keys = frozenset(value)
        expected_keys = (
            _STORED_DETAIL_KEYS_V1
            if durable_keys == _STORED_DETAIL_KEYS_V1
            else _STORED_DETAIL_KEYS_V2
        )
        durable_detail = _exact_mapping(value, expected_keys, "stored node detail")
        stored_truncated = durable_detail["truncated"]
        if not isinstance(stored_truncated, bool):
            raise WorkflowProgressStorageError(
                "stored node detail truncation evidence must be a boolean"
            )
        detail_keys = (
            _DETAIL_KEYS_V1 if expected_keys == _STORED_DETAIL_KEYS_V1 else _DETAIL_KEYS_V2
        )
        detail = {key: durable_detail[key] for key in detail_keys}
    else:
        detail_keys = frozenset(value)
        expected_keys = _DETAIL_KEYS_V1 if detail_keys == _DETAIL_KEYS_V1 else _DETAIL_KEYS_V2
        detail = _exact_mapping(value, expected_keys, "node detail")
    detail_schema_version = detail["schema_version"]
    if type(detail_schema_version) is not int or (
        (set(detail) == _DETAIL_KEYS_V1 and detail_schema_version != 1)
        or (
            set(detail) == _DETAIL_KEYS_V2
            and detail_schema_version != WORKFLOW_PROGRESS_NODE_DETAIL_SCHEMA_VERSION
        )
    ):
        raise WorkflowProgressStorageError("node detail schema_version is unsupported")
    node_id = _bounded_identity_text(
        detail["node_id"],
        "node detail node_id",
        max_bytes=WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
        enforce_current_policy=apply_current_text_policy,
    )
    invocation_identity, invocation_id = _normalize_invocation_identity(
        detail["invocation_identity"],
        identity=identity,
        enforce_current_policy=apply_current_text_policy,
    )
    state = detail["state"]
    if not isinstance(state, str) or state not in _NODE_STATES:
        raise WorkflowProgressStorageError("node detail state is unsupported")
    truncated = False

    progress_value = detail["progress"]
    progress: dict[str, Any] | None
    if progress_value is None:
        progress = None
    else:
        progress_input = _exact_mapping(progress_value, _PROGRESS_KEYS, "node progress")
        metrics, metrics_truncated = _metrics(
            progress_input["metrics"],
            "node progress metrics",
            apply_current_policy=apply_current_text_policy,
        )
        message, message_truncated = _bounded_redacted_text(
            progress_input["message"],
            "node progress message",
            max_bytes=WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES,
            nullable=True,
            apply_current_policy=apply_current_text_policy,
        )
        current = _finite_number(progress_input["current"], "node progress current", minimum=0.0)
        total = _finite_number(progress_input["total"], "node progress total", minimum=0.0)
        percent = _finite_number(
            progress_input["percent"],
            "node progress percent",
            minimum=0.0,
        )
        if percent > 100.0 or current > total:
            raise WorkflowProgressStorageError("node progress counters are inconsistent")
        expected_percent = 100.0 if total == 0.0 else round(current / total * 100.0, 1)
        if percent != expected_percent:
            raise WorkflowProgressStorageError(
                "node progress percent does not match current and total"
            )
        progress = {
            "current": current,
            "message": message,
            "metrics": metrics,
            "percent": expected_percent,
            "total": total,
            "updated_at": _timestamp(
                progress_input["updated_at"],
                "node progress updated_at",
            ),
        }
        truncated = truncated or metrics_truncated or message_truncated

    execution_value = detail["execution"]
    execution: dict[str, Any] | None
    if execution_value is None:
        execution = None
    else:
        execution_input = _exact_mapping(execution_value, _EXECUTION_KEYS, "node execution")
        assigned, resources_truncated = _assigned_resources(
            execution_input["assigned_resources"],
            "node assigned resources",
            apply_current_policy=apply_current_text_policy,
            allow_policy_omission=allow_current_policy_truncation,
        )
        execution = {
            name: _bounded_identity_text(
                execution_input[name],
                f"node execution {name}",
                max_bytes=WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
                nullable=True,
                enforce_current_policy=apply_current_text_policy,
            )
            for name in ("ray_job_id", "ray_node_id", "ray_task_id", "ray_worker_id")
        }
        execution["assigned_resources"] = assigned
        truncated = truncated or resources_truncated

    fanout = _normalize_fanout(detail["fanout"])

    output_preview: dict[str, Any] | None = None
    if detail_schema_version == WORKFLOW_PROGRESS_NODE_DETAIL_SCHEMA_VERSION:
        try:
            if enforce_current_preview_redaction:
                output_preview = validate_workflow_output_preview(detail["output_preview"])
            elif apply_current_text_policy:
                output_preview = read_workflow_output_preview(detail["output_preview"])
            else:
                output_preview = _validate_workflow_output_preview(
                    detail["output_preview"],
                    enforce_current_redaction=False,
                    apply_current_presentation=False,
                )
        except WorkflowOutputPreviewError as error:
            raise WorkflowProgressStorageError("node output preview is invalid") from error

    error, error_truncated = _bounded_redacted_text(
        detail["error"],
        "node error",
        max_bytes=WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES,
        nullable=True,
        apply_current_policy=apply_current_text_policy,
    )
    events_value = detail["recent_events"]
    if not isinstance(events_value, list):
        raise WorkflowProgressStorageError("recent_events must be a list")
    retained_events = events_value[-WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS:]
    if len(retained_events) != len(events_value):
        truncated = True
    events: list[dict[str, Any]] = []
    for event_value in retained_events:
        try:
            event, event_truncated = _normalize_event(
                event_value,
                apply_current_policy=apply_current_text_policy,
            )
        except WorkflowProgressStorageLimitError:
            truncated = True
            continue
        events.append(event)
        truncated = truncated or event_truncated
    events.sort(key=_event_sort_key)

    normalized = {
        "error": error,
        "execution": execution,
        "fanout": fanout,
        "finished_at": _timestamp(detail["finished_at"], "node finished_at", nullable=True),
        "invocation_identity": invocation_identity,
        "node_id": node_id,
        "progress": progress,
        "recent_events": events,
        "schema_version": detail_schema_version,
        "started_at": _timestamp(detail["started_at"], "node started_at", nullable=True),
        "state": state,
    }
    if output_preview is not None:
        normalized["output_preview"] = output_preview
    started_at = normalized["started_at"]
    finished_at = normalized["finished_at"]
    if state == "PENDING" and any(value is not None for value in (started_at, finished_at, error)):
        raise WorkflowProgressStorageError(
            "pending node detail cannot contain start, finish, or error data"
        )
    if state == "RUNNING" and (started_at is None or finished_at is not None or error is not None):
        raise WorkflowProgressStorageError("running node detail has inconsistent lifecycle data")
    if state == "SUCCEEDED" and (started_at is None or finished_at is None or error is not None):
        raise WorkflowProgressStorageError("successful node detail has inconsistent lifecycle data")
    if state == "FAILED" and (finished_at is None or error is None):
        raise WorkflowProgressStorageError("failed node detail requires finish and error data")
    if isinstance(started_at, str) and isinstance(finished_at, str):
        started = datetime.fromisoformat(started_at[:-1] + "+00:00")
        finished = datetime.fromisoformat(finished_at[:-1] + "+00:00")
        if finished < started:
            raise WorkflowProgressStorageError("node finished_at precedes started_at")
    if progress is not None:
        progress_updated_at = progress["updated_at"]
        if not isinstance(progress_updated_at, str):
            raise AssertionError("normalized progress timestamp must be text")
        updated = datetime.fromisoformat(progress_updated_at[:-1] + "+00:00")
        if isinstance(started_at, str):
            started = datetime.fromisoformat(started_at[:-1] + "+00:00")
            if updated < started:
                raise WorkflowProgressStorageError("node progress predates node start")
        if isinstance(finished_at, str):
            finished = datetime.fromisoformat(finished_at[:-1] + "+00:00")
            if updated > finished:
                raise WorkflowProgressStorageError("node progress follows node finish")
        if state == "SUCCEEDED" and progress["percent"] != 100.0:
            raise WorkflowProgressStorageError("successful node progress must be complete")
    if (
        state == "SUCCEEDED"
        and fanout is not None
        and (
            not fanout["input_exhausted"]
            or fanout["in_flight_items"] != 0
            or fanout["completed_items"] != fanout["submitted_items"]
        )
    ):
        raise WorkflowProgressStorageError("successful fanout node must be fully drained")
    if output_preview is not None:
        availability = output_preview["availability"]
        if state in {"SUCCEEDED", "FAILED"} and availability == "PENDING":
            raise WorkflowProgressStorageError("terminal node output preview cannot remain pending")
        if state != "SUCCEEDED" and availability in {"AVAILABLE", "REDACTED"}:
            raise WorkflowProgressStorageError(
                "non-successful node cannot contain an output preview value"
            )
    normalized_truncated = truncated or error_truncated
    if stored_truncated is not None:
        if normalized_truncated and not stored_truncated and not allow_current_policy_truncation:
            raise WorkflowProgressStorageError(
                "stored node detail suppresses deterministic truncation evidence"
            )
        normalized_truncated = stored_truncated or (
            allow_current_policy_truncation and normalized_truncated
        )
    return _prepared_node_detail(
        normalized,
        invocation_id=invocation_id,
        truncated=normalized_truncated,
    )


def prepare_workflow_progress_node_detail(
    value: Mapping[str, Any],
    *,
    identity: WorkflowRunIdentity,
) -> PreparedWorkflowProgressNodeDetail:
    """Normalize one bounded latest-state node record."""
    return _prepare_workflow_progress_node_detail(
        value,
        identity=identity,
        allow_stored_truncation=(
            isinstance(value, Mapping)
            and frozenset(value) in {_STORED_DETAIL_KEYS_V1, _STORED_DETAIL_KEYS_V2}
        ),
    )


def prepare_workflow_progress_detail(
    records: Iterable[Mapping[str, Any]],
    *,
    topology: PreparedWorkflowProgressTopology,
    reporting_policy: str = "full",
) -> PreparedWorkflowProgressDetail:
    """Prepare a deterministic bounded initial latest-state detail set."""
    if not isinstance(topology, PreparedWorkflowProgressTopology) or not (
        _prepared_topology_observed_membership_capability_matches(topology)
    ):
        raise WorkflowProgressStorageError(
            "initial detail requires package-issued observed topology membership"
        )
    if reporting_policy not in {"full", "sampled", "terminal_only", "disabled"}:
        raise WorkflowProgressStorageError("workflow reporting policy is unsupported")
    reasons: set[str] = set(topology.truncation_reasons)
    normalized: list[PreparedWorkflowProgressNodeDetail] = []
    seen: set[str] = set()
    node_kinds = dict(topology.node_kinds)
    for value in records:
        if not isinstance(value, Mapping):
            raise WorkflowProgressStorageError("node detail must be a mapping")
        detail_keys = frozenset(value)
        expected_keys = _DETAIL_KEYS_V1 if detail_keys == _DETAIL_KEYS_V1 else _DETAIL_KEYS_V2
        detail_value = _exact_mapping(value, expected_keys, "node detail")
        supplied_node_id = _bounded_identity_text(
            detail_value["node_id"],
            "node detail node_id",
            max_bytes=WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
        )
        if supplied_node_id is None:
            raise AssertionError("non-null node detail identity normalized to None")
        if supplied_node_id in seen:
            raise WorkflowProgressStorageError("node detail contains a duplicate node_id")
        seen.add(supplied_node_id)
        if supplied_node_id not in topology.observed_node_ids:
            raise WorkflowProgressStorageError("node detail references an unknown topology node_id")
        try:
            record = prepare_workflow_progress_node_detail(
                value,
                identity=topology.identity,
            )
        except WorkflowProgressStorageLimitError:
            reasons.add(WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
            continue
        if record.node_id != supplied_node_id:
            raise AssertionError("normalized node detail identity changed")
        if record.node_id not in topology.node_ids:
            continue
        decoded_record = json.loads(record.payload)
        has_fanout = decoded_record["fanout"] is not None
        if (node_kinds[record.node_id] == "map") != has_fanout:
            raise WorkflowProgressStorageError(
                "node detail fanout does not match the retained topology kind"
            )
        if record.truncated:
            reasons.add(WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
        normalized.append(record)
    if reporting_policy == "full" and seen != topology.observed_node_ids:
        raise WorkflowProgressStorageError(
            "full detail must contain one record per observed topology node"
        )
    if reporting_policy != "full":
        reasons.add(WorkflowProgressTruncationReason.REPORTING_POLICY.value)
    normalized.sort(key=lambda item: item.node_id)

    retained: list[PreparedWorkflowProgressNodeDetail] = []
    encoded_bytes = 0
    decoded_bytes = 0
    for record in normalized:
        if len(retained) >= WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS:
            reasons.add(WorkflowProgressTruncationReason.DETAIL_COUNT_LIMIT.value)
            break
        if encoded_bytes + record.encoded_bytes > WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES:
            reasons.add(WorkflowProgressTruncationReason.DETAIL_ENCODED_BYTES.value)
            break
        if decoded_bytes + record.decoded_bytes > WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES:
            reasons.add(WorkflowProgressTruncationReason.DETAIL_DECODED_BYTES.value)
            break
        if (
            topology.encoded_bytes + encoded_bytes + record.encoded_bytes
            > WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES
        ):
            reasons.add(WorkflowProgressTruncationReason.DETAIL_ENCODED_BYTES.value)
            break
        if (
            topology.decoded_bytes + decoded_bytes + record.decoded_bytes
            > WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES
        ):
            reasons.add(WorkflowProgressTruncationReason.DETAIL_DECODED_BYTES.value)
            break
        retained.append(record)
        encoded_bytes += record.encoded_bytes
        decoded_bytes += record.decoded_bytes

    # Apply the run-global event cap only after row admission. An omitted row
    # must never evict event history from a row that is actually retained.
    decoded_records: dict[str, dict[str, Any]] = {}
    event_entries: list[tuple[tuple[Any, ...], str, dict[str, Any]]] = []
    for record in retained:
        decoded = json.loads(record.payload)
        decoded_records[record.node_id] = decoded
        for occurrence, event in enumerate(decoded["recent_events"]):
            event_entries.append(
                (
                    _event_sort_key(
                        event,
                        node_id=record.node_id,
                        occurrence=occurrence,
                    ),
                    record.node_id,
                    event,
                )
            )
    selected_events = sorted(event_entries, key=lambda item: item[0])[
        -WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS:
    ]
    events_by_node: dict[str, list[dict[str, Any]]] = {}
    for _, node_id, event in selected_events:
        events_by_node.setdefault(node_id, []).append(event)

    event_bounded: list[PreparedWorkflowProgressNodeDetail] = []
    for record in retained:
        retained_events = sorted(events_by_node.get(record.node_id, []), key=_event_sort_key)
        if len(retained_events) != record.event_count:
            decoded = decoded_records[record.node_id]
            decoded["recent_events"] = retained_events
            record = _prepared_node_detail(
                decoded,
                invocation_id=record.invocation_id,
                truncated=True,
            )
            reasons.add(WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
        event_bounded.append(record)
    retained = event_bounded
    encoded_bytes = sum(record.encoded_bytes for record in retained)
    decoded_bytes = sum(record.decoded_bytes for record in retained)
    return PreparedWorkflowProgressDetail(
        records=tuple(retained),
        observed_count=topology.observed_node_count,
        encoded_bytes=encoded_bytes,
        decoded_bytes=decoded_bytes,
        truncation_reasons=tuple(sorted(reasons)),
    )


def _verify_prepared_topology(topology: PreparedWorkflowProgressTopology) -> None:
    _validate_run_identity(topology.identity)
    if (
        type(topology.topology_version) is not int
        or topology.topology_version <= 0
        or topology.topology_version > WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER
    ):
        raise WorkflowProgressStorageError("prepared topology version is invalid")
    if len(topology.manifest_payload) > WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES:
        raise WorkflowProgressStorageLimitError("prepared topology manifest is oversized")
    if _digest(_MANIFEST_DOMAIN, topology.manifest_payload) != topology.manifest_digest:
        raise WorkflowProgressStorageError("prepared topology manifest digest is invalid")

    rank = {
        WorkflowProgressTopologyCollection.NODE: 0,
        WorkflowProgressTopologyCollection.EDGE: 1,
    }
    pages = list(topology.pages)
    if pages != sorted(pages, key=lambda page: (rank.get(page.collection, 99), page.page_index)):
        raise WorkflowProgressStorageError("prepared topology pages are not canonically ordered")
    next_index = {
        WorkflowProgressTopologyCollection.NODE: 0,
        WorkflowProgressTopologyCollection.EDGE: 0,
    }
    node_records: list[dict[str, Any]] = []
    edge_records: list[dict[str, Any]] = []
    for page in pages:
        if page.collection not in rank:
            raise WorkflowProgressStorageError("prepared topology page collection is invalid")
        if page.page_index != next_index[page.collection]:
            raise WorkflowProgressStorageError("prepared topology page indexes are not contiguous")
        next_index[page.collection] += 1
        if not 1 <= page.item_count <= WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS:
            raise WorkflowProgressStorageLimitError("prepared topology page item count is invalid")
        if (
            len(page.payload) != page.encoded_bytes
            or len(page.payload) != page.decoded_bytes
            or page.encoded_bytes > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES
            or page.decoded_bytes > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES
        ):
            raise WorkflowProgressStorageLimitError("prepared topology page sizes are invalid")
        if _digest(_PAGE_DOMAIN, page.payload) != page.digest:
            raise WorkflowProgressStorageError("prepared topology page digest is invalid")
        try:
            value = _decode_canonical_payload(page.payload, "prepared topology page")
        except WorkflowProgressStorageIntegrityError as error:
            raise WorkflowProgressStorageError(str(error)) from error
        if (
            not isinstance(value, dict)
            or set(value) != {"collection", "records", "schema_version"}
            or value["collection"] != page.collection.value
            or type(value["schema_version"]) is not int
            or value["schema_version"] != WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION
            or not isinstance(value["records"], list)
            or len(value["records"]) != page.item_count
        ):
            raise WorkflowProgressStorageError("prepared topology page envelope is invalid")
        if page.collection is WorkflowProgressTopologyCollection.NODE:
            for record in value["records"]:
                normalized, _ = _normalize_topology_node(record)
                if normalized != record:
                    raise WorkflowProgressStorageError("prepared topology node is not normalized")
                node_records.append(normalized)
        else:
            for record in value["records"]:
                normalized = _normalize_topology_edge(record)
                if normalized != record:
                    raise WorkflowProgressStorageError("prepared topology edge is not normalized")
                edge_records.append(normalized)

    node_ids = [str(record["node_id"]) for record in node_records]
    if node_ids != sorted(node_ids) or len(node_ids) != len(set(node_ids)):
        raise WorkflowProgressStorageError("prepared topology node order is invalid")
    edges = [(record["source"], record["target"]) for record in edge_records]
    if edges != sorted(edges) or len(edges) != len(set(edges)):
        raise WorkflowProgressStorageError("prepared topology edge order is invalid")
    retained_node_ids = frozenset(node_ids)
    if any(
        source not in retained_node_ids or target not in retained_node_ids
        for source, target in edges
    ):
        raise WorkflowProgressStorageError("prepared topology edge references an omitted node")

    expected_descriptors = [
        {
            "collection": page.collection.value,
            "decoded_bytes": page.decoded_bytes,
            "digest": page.digest,
            "encoding": "identity",
            "encoded_bytes": page.encoded_bytes,
            "item_count": page.item_count,
            "page_index": page.page_index,
        }
        for page in pages
    ]
    try:
        manifest = _decode_canonical_payload(
            topology.manifest_payload,
            "prepared topology manifest",
        )
    except WorkflowProgressStorageIntegrityError as error:
        raise WorkflowProgressStorageError(str(error)) from error
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "edge_count",
            "node_count",
            "pages",
            "run_identity",
            "schema_version",
            "topology_version",
            "truncation_reasons",
        }
        or manifest["schema_version"] != WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION
        or type(manifest["schema_version"]) is not int
        or manifest["run_identity"] != topology.identity.as_dict()
        or manifest["topology_version"] != topology.topology_version
        or type(manifest["topology_version"]) is not int
        or manifest["truncation_reasons"] != list(topology.truncation_reasons)
        or manifest["node_count"] != len(node_records)
        or type(manifest["node_count"]) is not int
        or manifest["edge_count"] != len(edge_records)
        or type(manifest["edge_count"]) is not int
        or manifest["pages"] != expected_descriptors
    ):
        raise WorkflowProgressStorageError("prepared topology manifest envelope is invalid")

    encoded_bytes = len(topology.manifest_payload) + sum(page.encoded_bytes for page in pages)
    decoded_bytes = len(topology.manifest_payload) + sum(page.decoded_bytes for page in pages)
    if (
        topology.node_ids != retained_node_ids
        or not retained_node_ids.issubset(topology.observed_node_ids)
        or topology.node_kinds
        != tuple((str(record["node_id"]), str(record["kind"])) for record in node_records)
        or topology.map_node_ids
        != frozenset(str(record["node_id"]) for record in node_records if record["kind"] == "map")
        or topology.edges
        != tuple((str(record["source"]), str(record["target"])) for record in edge_records)
        or topology.retained_node_count != len(node_records)
        or topology.retained_edge_count != len(edge_records)
        or topology.observed_node_count < len(topology.observed_node_ids)
        or topology.observed_edge_count < len(edge_records)
        or topology.encoded_bytes != encoded_bytes
        or topology.decoded_bytes != decoded_bytes
    ):
        raise WorkflowProgressStorageError("prepared topology evidence is inconsistent")
    if encoded_bytes > WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES:
        raise WorkflowProgressStorageLimitError("prepared topology exceeds the encoded limit")
    if decoded_bytes > WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES:
        raise WorkflowProgressStorageLimitError("prepared topology exceeds the decoded limit")
    valid_reasons = {reason.value for reason in WorkflowProgressTruncationReason}
    if any(
        not isinstance(reason, str) or reason not in valid_reasons
        for reason in topology.truncation_reasons
    ) or topology.truncation_reasons != tuple(sorted(set(topology.truncation_reasons))):
        raise WorkflowProgressStorageError("prepared topology truncation reasons are invalid")


def _verify_prepared_node_detail(
    record: PreparedWorkflowProgressNodeDetail,
    *,
    identity: WorkflowRunIdentity,
) -> None:
    if (
        not isinstance(record.truncated, bool)
        or len(record.payload) != record.encoded_bytes
        or len(record.payload) != record.decoded_bytes
        or record.encoded_bytes > WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES
        or not 0 <= record.event_count <= WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS
        or _digest(_DETAIL_DOMAIN, record.payload) != record.digest
    ):
        raise WorkflowProgressStorageError("prepared node detail evidence is inconsistent")
    try:
        value = _decode_canonical_payload(record.payload, "prepared node detail")
    except WorkflowProgressStorageIntegrityError as error:
        raise WorkflowProgressStorageError(str(error)) from error
    if not isinstance(value, dict):
        raise WorkflowProgressStorageError("prepared node detail must be an object")
    normalized = _prepare_workflow_progress_node_detail(
        value,
        identity=identity,
        allow_stored_truncation=True,
    )
    if (
        normalized.node_id != record.node_id
        or normalized.node_key != record.node_key
        or normalized.state != record.state
        or normalized.invocation_id != record.invocation_id
        or normalized.truncated != record.truncated
        or normalized.payload != record.payload
        or normalized.digest != record.digest
        or normalized.encoded_bytes != record.encoded_bytes
        or normalized.decoded_bytes != record.decoded_bytes
        or normalized.event_count != record.event_count
    ):
        raise WorkflowProgressStorageError("prepared node detail is not normalized")


def _verify_prepared_detail(
    detail: PreparedWorkflowProgressDetail,
    *,
    identity: WorkflowRunIdentity,
) -> None:
    if (
        type(detail.observed_count) is not int
        or detail.observed_count < len(detail.records)
        or detail.observed_count > WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER
        or len(detail.records) > WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS
        or detail.encoded_bytes != sum(record.encoded_bytes for record in detail.records)
        or detail.decoded_bytes != sum(record.decoded_bytes for record in detail.records)
        or detail.encoded_bytes > WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES
        or detail.decoded_bytes > WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES
        or any(not isinstance(reason, str) for reason in detail.truncation_reasons)
        or detail.truncation_reasons != tuple(sorted(set(detail.truncation_reasons)))
    ):
        raise WorkflowProgressStorageError("prepared workflow detail evidence is inconsistent")
    _encode_truncation_reasons(detail.truncation_reasons)
    seen: set[str] = set()
    for record in detail.records:
        _verify_prepared_node_detail(record, identity=identity)
        if record.node_id in seen:
            raise WorkflowProgressStorageError("prepared workflow detail repeats a node")
        seen.add(record.node_id)


def _manifest_uuid(value: Any) -> str:
    if not isinstance(value, str):
        value = str(value)
    try:
        normalized = str(UUID(value))
    except (AttributeError, ValueError) as error:
        raise WorkflowProgressStorageError("manifest_id must be a canonical UUID") from error
    if normalized != value:
        raise WorkflowProgressStorageError("manifest_id must be a canonical UUID")
    return normalized


def _bounded_manifest_descriptors(
    manifest: Any,
    row: Mapping[str, Any],
    *,
    identity: WorkflowRunIdentity,
    payload_octets: int,
) -> tuple[list[dict[str, Any]], int, int, int, tuple[str, ...]]:
    """Validate manifest-owned bounds before any linked page blob is selected."""
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "edge_count",
            "node_count",
            "pages",
            "run_identity",
            "schema_version",
            "topology_version",
            "truncation_reasons",
        }
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION
        or manifest["run_identity"] != identity.as_dict()
        or type(manifest["topology_version"]) is not int
        or manifest["topology_version"] != row["topology_version"]
        or type(manifest["node_count"]) is not int
        or not 0 <= manifest["node_count"] <= WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS
        or type(manifest["edge_count"]) is not int
        or not 0 <= manifest["edge_count"] <= WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS
        or not isinstance(manifest["truncation_reasons"], list)
        or not isinstance(manifest["pages"], list)
    ):
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology manifest envelope is invalid"
        )
    try:
        payload_reasons = _encode_truncation_reasons(manifest["truncation_reasons"])
    except WorkflowProgressStorageError as error:
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology truncation reasons are invalid"
        ) from error
    if payload_reasons != row["truncation_reasons"]:
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology truncation evidence is not authenticated"
        )
    scalar_values = (
        row["node_count"],
        row["edge_count"],
        row["node_page_count"],
        row["edge_page_count"],
        row["encoded_bytes"],
        row["decoded_bytes"],
    )
    if any(type(value) is not int or value < 0 for value in scalar_values):
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology relational metadata is invalid"
        )
    if (
        row["node_count"] != manifest["node_count"]
        or row["edge_count"] != manifest["edge_count"]
        or row["node_count"] > WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS
        or row["edge_count"] > WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS
        or row["encoded_bytes"] < payload_octets
        or row["encoded_bytes"] > WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES
        or row["decoded_bytes"] < payload_octets
        or row["decoded_bytes"] > WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES
    ):
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology relational metadata is invalid"
        )

    descriptors: list[dict[str, Any]] = []
    expected_indexes = {
        WorkflowProgressTopologyCollection.NODE.value: 0,
        WorkflowProgressTopologyCollection.EDGE.value: 0,
    }
    item_counts = {
        WorkflowProgressTopologyCollection.NODE.value: 0,
        WorkflowProgressTopologyCollection.EDGE.value: 0,
    }
    page_encoded_bytes = 0
    page_decoded_bytes = 0
    for value in manifest["pages"]:
        if not isinstance(value, dict) or set(value) != _TOPOLOGY_PAGE_DESCRIPTOR_KEYS:
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology page descriptor is invalid"
            )
        descriptor = dict(value)
        collection = descriptor["collection"]
        digest = descriptor["digest"]
        if (
            collection not in expected_indexes
            or type(descriptor["page_index"]) is not int
            or descriptor["page_index"] != expected_indexes[collection]
            or descriptor["encoding"] != WorkflowProgressTopologyEncoding.IDENTITY
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or type(descriptor["item_count"]) is not int
            or not 1 <= descriptor["item_count"] <= WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS
            or type(descriptor["encoded_bytes"]) is not int
            or not 1
            <= descriptor["encoded_bytes"]
            <= WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES
            or type(descriptor["decoded_bytes"]) is not int
            or descriptor["decoded_bytes"] != descriptor["encoded_bytes"]
            or descriptor["decoded_bytes"] > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES
        ):
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology page descriptor is invalid"
            )
        expected_indexes[collection] += 1
        item_counts[collection] += descriptor["item_count"]
        page_encoded_bytes += descriptor["encoded_bytes"]
        page_decoded_bytes += descriptor["decoded_bytes"]
        if (
            payload_octets + page_encoded_bytes > WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES
            or payload_octets + page_decoded_bytes > WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES
        ):
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology manifest exceeds aggregate limits"
            )
        descriptors.append(descriptor)

    if (
        expected_indexes[WorkflowProgressTopologyCollection.NODE.value] != row["node_page_count"]
        or expected_indexes[WorkflowProgressTopologyCollection.EDGE.value] != row["edge_page_count"]
        or item_counts[WorkflowProgressTopologyCollection.NODE.value] != row["node_count"]
        or item_counts[WorkflowProgressTopologyCollection.EDGE.value] != row["edge_count"]
        or payload_octets + page_encoded_bytes != row["encoded_bytes"]
        or payload_octets + page_decoded_bytes != row["decoded_bytes"]
    ):
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology manifest bounds conflict with relational metadata"
        )
    return (
        descriptors,
        page_encoded_bytes,
        page_decoded_bytes,
        len(descriptors),
        _decode_truncation_reasons(payload_reasons, stored=True),
    )


def verify_workflow_progress_topology_manifest_record(
    row: Mapping[str, Any],
    *,
    expected_identity: WorkflowRunIdentity | None = None,
) -> VerifiedWorkflowProgressTopologyManifestRecord:
    """Verify one bounded manifest projection without reading linked pages."""
    payload_octets = row.get("_payload_octets")
    if (
        type(payload_octets) is not int
        or payload_octets <= 0
        or payload_octets > WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES
        or row.get("_bounded_payload") is None
    ):
        raise WorkflowProgressStorageIntegrityError("workflow topology manifest is oversized")
    payload = _as_bytes(row["_bounded_payload"], "workflow topology manifest")
    if len(payload) != payload_octets:
        raise WorkflowProgressStorageIntegrityError("workflow topology manifest length changed")

    try:
        identity = WorkflowRunIdentity(
            task_execution_pk=row["run_storage__execution_id"],
            attempt_number=row["run_storage__attempt_number"],
            execution_generation=row["run_storage__execution_generation"],
            run_id=str(row["run_storage__run_id"]),
        )
        _validate_run_identity(identity)
    except (KeyError, WorkflowProgressStorageError) as error:
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology run identity is invalid"
        ) from error
    if expected_identity is not None and identity != expected_identity:
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology manifest belongs to another run"
        )
    if _digest(_MANIFEST_DOMAIN, payload) != row.get("manifest_digest"):
        raise WorkflowProgressStorageIntegrityError("workflow topology manifest digest is invalid")
    manifest = _decode_canonical_payload(payload, "workflow topology manifest")
    (
        manifest_descriptors,
        expected_page_encoded_bytes,
        expected_page_decoded_bytes,
        expected_link_count,
        authenticated_reasons,
    ) = _bounded_manifest_descriptors(
        manifest,
        row,
        identity=identity,
        payload_octets=payload_octets,
    )
    slot = row.get("slot")
    published_at = row.get("published_at")
    if (
        slot not in {WorkflowProgressTopologySlot.CURRENT, WorkflowProgressTopologySlot.PENDING}
        or (slot == WorkflowProgressTopologySlot.CURRENT and published_at is None)
        or (slot == WorkflowProgressTopologySlot.PENDING and published_at is not None)
    ):
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology manifest publication state is invalid"
        )
    return VerifiedWorkflowProgressTopologyManifestRecord(
        identity=identity,
        topology_version=row["topology_version"],
        slot=slot,
        node_count=row["node_count"],
        edge_count=row["edge_count"],
        page_descriptors=tuple(manifest_descriptors),
        page_encoded_bytes=expected_page_encoded_bytes,
        page_decoded_bytes=expected_page_decoded_bytes,
        expected_link_count=expected_link_count,
        truncation_reasons=authenticated_reasons,
    )


def verify_workflow_progress_topology_page_record(
    row: Mapping[str, Any],
    *,
    descriptor: Mapping[str, Any],
    expected_run_storage_id: int,
) -> tuple[dict[str, Any], ...]:
    """Verify one manifest-linked topology page and return detached records."""
    if not isinstance(descriptor, Mapping):
        raise WorkflowProgressStorageIntegrityError("workflow topology page descriptor is invalid")
    collection = descriptor.get("collection")
    if (
        set(descriptor) != _TOPOLOGY_PAGE_DESCRIPTOR_KEYS
        or collection
        not in {
            WorkflowProgressTopologyCollection.NODE.value,
            WorkflowProgressTopologyCollection.EDGE.value,
        }
        or row.get("collection") != collection
        or row.get("page_index") != descriptor.get("page_index")
        or row.get("page__run_storage_id") != expected_run_storage_id
        or row.get("page__collection") != collection
        or row.get("page__encoding") != WorkflowProgressTopologyEncoding.IDENTITY
        or row.get("page__digest") != descriptor.get("digest")
        or row.get("page__item_count") != descriptor.get("item_count")
        or row.get("page__encoded_bytes") != descriptor.get("encoded_bytes")
        or row.get("page__decoded_bytes") != descriptor.get("decoded_bytes")
    ):
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology page ownership or metadata is invalid"
        )
    page_octets = row.get("_payload_octets")
    if (
        type(page_octets) is not int
        or page_octets <= 0
        or page_octets > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES
        or page_octets > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES
        or row.get("_bounded_payload") is None
    ):
        raise WorkflowProgressStorageIntegrityError("workflow topology page is oversized")
    page_payload = _as_bytes(row["_bounded_payload"], "workflow topology page")
    if (
        len(page_payload) != page_octets
        or page_octets != descriptor["encoded_bytes"]
        or page_octets != descriptor["decoded_bytes"]
        or _digest(_PAGE_DOMAIN, page_payload) != descriptor["digest"]
    ):
        raise WorkflowProgressStorageIntegrityError("workflow topology page metadata is invalid")
    page = _decode_canonical_payload(page_payload, "workflow topology page")
    if (
        not isinstance(page, dict)
        or set(page) != {"collection", "records", "schema_version"}
        or page["collection"] != collection
        or type(page["schema_version"]) is not int
        or page["schema_version"] != WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION
        or not isinstance(page["records"], list)
        or len(page["records"]) != descriptor["item_count"]
    ):
        raise WorkflowProgressStorageIntegrityError("workflow topology page envelope is invalid")
    records: list[dict[str, Any]] = []
    try:
        if collection == WorkflowProgressTopologyCollection.NODE.value:
            for item in page["records"]:
                authenticated, _ = _normalize_topology_node(
                    item,
                    apply_current_policy=False,
                )
                if authenticated != item:
                    raise WorkflowProgressStorageIntegrityError(
                        "workflow topology node is not canonically stored"
                    )
                presented, _ = _normalize_topology_node(authenticated)
                if presented["node_id"] != authenticated["node_id"]:
                    raise WorkflowProgressStorageIntegrityError(
                        "workflow topology node identity is unsafe"
                    )
                records.append(presented)
            stable_keys = [str(item["node_id"]) for item in records]
        else:
            for item in page["records"]:
                authenticated = _normalize_topology_edge(
                    item,
                    apply_current_policy=False,
                )
                if authenticated != item:
                    raise WorkflowProgressStorageIntegrityError(
                        "workflow topology edge is not canonically stored"
                    )
                presented = _normalize_topology_edge(authenticated)
                if presented != authenticated:
                    raise WorkflowProgressStorageIntegrityError(
                        "workflow topology edge identity is unsafe"
                    )
                records.append(presented)
            stable_keys = [(str(item["source"]), str(item["target"])) for item in records]
    except WorkflowProgressStorageError as error:
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology page record is invalid"
        ) from error
    if stable_keys != sorted(stable_keys) or len(stable_keys) != len(set(stable_keys)):
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology page record order is invalid"
        )
    return tuple(records)


def verify_workflow_progress_topology_manifest(
    manifest_id: str,
    *,
    expected_identity: WorkflowRunIdentity | None = None,
    using: str = "default",
) -> VerifiedWorkflowProgressTopology:
    """Read and verify one manifest without transferring oversized binary values."""
    manifest_id = _manifest_uuid(manifest_id)
    manifest_query = (
        WorkflowProgressTopologyManifest.objects.using(using)
        .filter(pk=manifest_id)
        .annotate(_payload_octets=_BlobOctetLength("payload"))
        .annotate(
            _bounded_payload=Case(
                When(
                    _payload_octets__lte=WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES,
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
        )
    )
    row = manifest_query.first()
    if row is None:
        raise WorkflowProgressStorageIntegrityError("workflow topology manifest is missing")
    verified_manifest = verify_workflow_progress_topology_manifest_record(
        row,
        expected_identity=expected_identity,
    )
    payload_octets = row["_payload_octets"]
    manifest_descriptors = list(verified_manifest.page_descriptors)
    expected_page_encoded_bytes = verified_manifest.page_encoded_bytes
    expected_page_decoded_bytes = verified_manifest.page_decoded_bytes
    expected_link_count = verified_manifest.expected_link_count
    authenticated_reasons = verified_manifest.truncation_reasons

    link_query = WorkflowProgressTopologyManifestPage.objects.using(using).filter(
        manifest_id=manifest_id
    )
    link_stats = link_query.aggregate(
        _link_count=Count("pk"),
        _payload_octets=Sum(_BlobOctetLength("page__payload")),
        _encoded_bytes=Sum("page__encoded_bytes"),
        _decoded_bytes=Sum("page__decoded_bytes"),
        _item_count=Sum("page__item_count"),
    )
    actual_page_octets = link_stats["_payload_octets"] or 0
    if (
        actual_page_octets > expected_page_encoded_bytes
        or payload_octets + actual_page_octets > WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES
    ):
        raise WorkflowProgressStorageIntegrityError("workflow topology page is oversized")
    if (
        link_stats["_link_count"] != expected_link_count
        or actual_page_octets != expected_page_encoded_bytes
        or (link_stats["_encoded_bytes"] or 0) != expected_page_encoded_bytes
        or (link_stats["_decoded_bytes"] or 0) != expected_page_decoded_bytes
        or (link_stats["_item_count"] or 0) != row["node_count"] + row["edge_count"]
    ):
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology linked-page aggregates are invalid"
        )

    rank = Case(
        When(collection=WorkflowProgressTopologyCollection.NODE.value, then=Value(0)),
        When(collection=WorkflowProgressTopologyCollection.EDGE.value, then=Value(1)),
        default=Value(2),
        output_field=IntegerField(),
    )
    links = (
        link_query.annotate(_payload_octets=_BlobOctetLength("page__payload"), _rank=rank)
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
        .order_by("_rank", "page_index")
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
        )[: expected_link_count + 1]
        .iterator(chunk_size=1)
    )
    expected_indexes = {
        WorkflowProgressTopologyCollection.NODE.value: 0,
        WorkflowProgressTopologyCollection.EDGE.value: 0,
    }
    descriptors: list[dict[str, Any]] = []
    node_records: list[dict[str, Any]] = []
    edge_records: list[dict[str, Any]] = []
    page_encoded_bytes = 0
    page_decoded_bytes = 0
    link_count = 0
    for link in links:
        link_count += 1
        if link_count > expected_link_count:
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology has too many linked pages"
            )
        collection = link["collection"]
        if collection not in expected_indexes:
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology link collection is invalid"
            )
        if (
            link["page_index"] != expected_indexes[collection]
            or link["page__collection"] != collection
            or link["page__run_storage_id"] != row["run_storage_id"]
            or link["page__encoding"] != WorkflowProgressTopologyEncoding.IDENTITY
        ):
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology link ownership or order is invalid"
            )
        expected_indexes[collection] += 1
        page_octets = link["_payload_octets"]
        if (
            type(page_octets) is not int
            or page_octets <= 0
            or page_octets > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES
            or page_octets > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES
            or link["_bounded_payload"] is None
        ):
            raise WorkflowProgressStorageIntegrityError("workflow topology page is oversized")
        if (
            payload_octets + page_encoded_bytes + page_octets
            > WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES
            or payload_octets + page_decoded_bytes + page_octets
            > WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES
        ):
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology linked pages exceed aggregate limits"
            )
        page_payload = _as_bytes(link["_bounded_payload"], "workflow topology page")
        if (
            len(page_payload) != page_octets
            or link["page__encoded_bytes"] != page_octets
            or link["page__decoded_bytes"] != page_octets
            or not 1 <= link["page__item_count"] <= WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS
            or _digest(_PAGE_DOMAIN, page_payload) != link["page__digest"]
        ):
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology page metadata is invalid"
            )
        page = _decode_canonical_payload(page_payload, "workflow topology page")
        if (
            not isinstance(page, dict)
            or set(page) != {"collection", "records", "schema_version"}
            or page["collection"] != collection
            or type(page["schema_version"]) is not int
            or page["schema_version"] != WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION
            or not isinstance(page["records"], list)
            or len(page["records"]) != link["page__item_count"]
        ):
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology page envelope is invalid"
            )
        try:
            if collection == WorkflowProgressTopologyCollection.NODE.value:
                for item in page["records"]:
                    authenticated, _ = _normalize_topology_node(
                        item,
                        apply_current_policy=False,
                    )
                    if authenticated != item:
                        raise WorkflowProgressStorageIntegrityError(
                            "workflow topology node is not canonically stored"
                        )
                    presented, _ = _normalize_topology_node(authenticated)
                    if presented["node_id"] != authenticated["node_id"]:
                        raise WorkflowProgressStorageIntegrityError(
                            "workflow topology node identity is unsafe"
                        )
                    node_records.append(presented)
            else:
                for item in page["records"]:
                    authenticated = _normalize_topology_edge(
                        item,
                        apply_current_policy=False,
                    )
                    if authenticated != item:
                        raise WorkflowProgressStorageIntegrityError(
                            "workflow topology edge is not canonically stored"
                        )
                    presented = _normalize_topology_edge(authenticated)
                    if presented != authenticated:
                        raise WorkflowProgressStorageIntegrityError(
                            "workflow topology edge identity is unsafe"
                        )
                    edge_records.append(presented)
        except WorkflowProgressStorageError as error:
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology page record is invalid"
            ) from error
        descriptors.append(
            {
                "collection": collection,
                "decoded_bytes": page_octets,
                "digest": link["page__digest"],
                "encoding": WorkflowProgressTopologyEncoding.IDENTITY,
                "encoded_bytes": page_octets,
                "item_count": link["page__item_count"],
                "page_index": link["page_index"],
            }
        )
        page_encoded_bytes += page_octets
        page_decoded_bytes += page_octets

    node_ids = [str(item["node_id"]) for item in node_records]
    edges = [(str(item["source"]), str(item["target"])) for item in edge_records]
    node_id_set = frozenset(node_ids)
    if (
        node_ids != sorted(node_ids)
        or len(node_ids) != len(node_id_set)
        or edges != sorted(edges)
        or len(edges) != len(set(edges))
        or any(source not in node_id_set or target not in node_id_set for source, target in edges)
    ):
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology record order or identity is invalid"
        )

    if (
        link_count != expected_link_count
        or row["node_count"] != len(node_records)
        or row["edge_count"] != len(edge_records)
        or manifest_descriptors != descriptors
    ):
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology manifest envelope is invalid"
        )
    encoded_bytes = payload_octets + page_encoded_bytes
    decoded_bytes = payload_octets + page_decoded_bytes
    if (
        row["node_count"] != len(node_records)
        or row["edge_count"] != len(edge_records)
        or row["node_page_count"] != expected_indexes[WorkflowProgressTopologyCollection.NODE.value]
        or row["edge_page_count"] != expected_indexes[WorkflowProgressTopologyCollection.EDGE.value]
        or row["encoded_bytes"] != encoded_bytes
        or row["decoded_bytes"] != decoded_bytes
        or encoded_bytes > WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES
        or decoded_bytes > WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES
        or (row["slot"] == WorkflowProgressTopologySlot.PENDING and row["published_at"] is not None)
        or (row["slot"] == WorkflowProgressTopologySlot.CURRENT and row["published_at"] is None)
        or row["slot"]
        not in {WorkflowProgressTopologySlot.PENDING, WorkflowProgressTopologySlot.CURRENT}
    ):
        raise WorkflowProgressStorageIntegrityError(
            "workflow topology relational metadata is invalid"
        )
    return VerifiedWorkflowProgressTopology(
        manifest_id=manifest_id,
        run_storage_id=row["run_storage_id"],
        topology_version=row["topology_version"],
        slot=row["slot"],
        node_ids=node_id_set,
        node_kinds=tuple((str(item["node_id"]), str(item["kind"])) for item in node_records),
        edges=tuple(edges),
        node_count=len(node_records),
        edge_count=len(edge_records),
        encoded_bytes=encoded_bytes,
        decoded_bytes=decoded_bytes,
        truncation_reasons=authenticated_reasons,
        map_node_ids=frozenset(
            str(item["node_id"]) for item in node_records if item["kind"] == "map"
        ),
    )


def _stored_page_matches(
    page_id: int,
    prepared: PreparedWorkflowProgressTopologyPage,
    *,
    run_storage_id: int,
    using: str,
) -> bool:
    row = (
        WorkflowProgressTopologyPage.objects.using(using)
        .filter(pk=page_id)
        .annotate(_payload_octets=_BlobOctetLength("payload"))
        .annotate(
            _bounded_payload=Case(
                When(
                    _payload_octets__lte=WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES,
                    then=F("payload"),
                ),
                default=Value(None),
                output_field=BinaryField(),
            )
        )
        .values(
            "run_storage_id",
            "digest",
            "collection",
            "encoding",
            "item_count",
            "encoded_bytes",
            "decoded_bytes",
            "_payload_octets",
            "_bounded_payload",
        )
        .first()
    )
    if row is None:
        return False
    octets = row["_payload_octets"]
    if (
        type(octets) is not int
        or octets <= 0
        or octets > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES
        or octets > WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES
        or row["_bounded_payload"] is None
    ):
        raise WorkflowProgressStorageIntegrityError("stored topology page is oversized")
    payload = _as_bytes(row["_bounded_payload"], "stored topology page")
    return (
        row["run_storage_id"] == run_storage_id
        and row["digest"] == prepared.digest
        and row["collection"] == prepared.collection.value
        and row["encoding"] == WorkflowProgressTopologyEncoding.IDENTITY
        and row["item_count"] == prepared.item_count
        and row["encoded_bytes"] == prepared.encoded_bytes
        and row["decoded_bytes"] == prepared.decoded_bytes
        and octets == len(payload) == len(prepared.payload)
        and payload == prepared.payload
    )


def _stored_manifest_matches_prepared(
    manifest: WorkflowProgressTopologyManifest,
    topology: PreparedWorkflowProgressTopology,
    *,
    using: str,
) -> bool:
    if (
        manifest.topology_version != topology.topology_version
        or manifest.manifest_digest != topology.manifest_digest
        or manifest.node_count != topology.retained_node_count
        or manifest.edge_count != topology.retained_edge_count
        or manifest.truncation_reasons != _encode_truncation_reasons(topology.truncation_reasons)
        or manifest.encoded_bytes != topology.encoded_bytes
        or manifest.decoded_bytes != topology.decoded_bytes
    ):
        return False
    verified = verify_workflow_progress_topology_manifest(
        str(manifest.pk),
        expected_identity=topology.identity,
        using=using,
    )
    return (
        verified.node_ids == topology.node_ids
        and verified.node_kinds == topology.node_kinds
        and verified.edges == topology.edges
        and verified.encoded_bytes == topology.encoded_bytes
        and verified.decoded_bytes == topology.decoded_bytes
        and verified.truncation_reasons == topology.truncation_reasons
    )


def _trusted_current_topology(
    manifest: WorkflowProgressTopologyManifest,
    topology: PreparedWorkflowProgressTopology,
    *,
    run_storage_id: int,
) -> VerifiedWorkflowProgressTopology:
    """Bind already-verified immutable topology evidence without rereading its pages."""
    if (
        manifest.run_storage_id != run_storage_id
        or manifest.slot != WorkflowProgressTopologySlot.CURRENT
        or manifest.published_at is None
        or manifest.topology_version != topology.topology_version
        or manifest.manifest_digest != topology.manifest_digest
        or manifest.node_count != topology.retained_node_count
        or manifest.edge_count != topology.retained_edge_count
        or manifest.truncation_reasons != _encode_truncation_reasons(topology.truncation_reasons)
        or manifest.encoded_bytes != topology.encoded_bytes
        or manifest.decoded_bytes != topology.decoded_bytes
    ):
        raise WorkflowProgressStorageIntegrityError(
            "current workflow topology conflicts with prepared immutable evidence"
        )
    return VerifiedWorkflowProgressTopology(
        manifest_id=str(manifest.pk),
        run_storage_id=run_storage_id,
        topology_version=topology.topology_version,
        slot=manifest.slot,
        node_ids=topology.node_ids,
        node_kinds=topology.node_kinds,
        edges=topology.edges,
        node_count=topology.retained_node_count,
        edge_count=topology.retained_edge_count,
        encoded_bytes=topology.encoded_bytes,
        decoded_bytes=topology.decoded_bytes,
        truncation_reasons=topology.truncation_reasons,
        map_node_ids=topology.map_node_ids,
    )


def _validate_prepared_topology_reference(
    topology: PreparedWorkflowProgressTopology,
) -> None:
    """Validate package-issued immutable evidence, rechecking after transfer.

    Normal in-process sparse flushes take the constant-time capability path. A
    copied, deserialized, or manually constructed value is fully revalidated once
    before receiving a process-local capability; changing any immutable evidence
    object invalidates that capability.
    """
    if not isinstance(topology, PreparedWorkflowProgressTopology):
        raise WorkflowProgressStorageError(
            "prepared topology must use the package-owned evidence type"
        )
    if _prepared_topology_capability_matches(topology):
        return
    _verify_prepared_topology(topology)
    _register_prepared_topology_capability(topology)


def stage_workflow_progress_topology(
    topology: PreparedWorkflowProgressTopology,
    *,
    using: str = "default",
) -> str | None:
    """Stage at most one verified bounded topology candidate for an exact run."""
    _verify_prepared_topology(topology)
    _register_prepared_topology_capability(topology)
    identity = topology.identity
    exact_execution = {
        "pk": identity.task_execution_pk,
        "state": TaskState.RUNNING,
        "attempt_number": identity.attempt_number,
        "execution_generation": identity.execution_generation,
        "workflow_run_id": identity.run_id,
    }
    if not RayTaskExecution.objects.using(using).filter(**exact_execution).exists():
        return None

    with transaction.atomic(using=using):
        run_storage, _ = WorkflowProgressRunStorage.objects.using(using).get_or_create(
            execution_id=identity.task_execution_pk,
            attempt_number=identity.attempt_number,
            execution_generation=identity.execution_generation,
            run_id=identity.run_id,
        )
        run_storage = (
            WorkflowProgressRunStorage.objects.using(using)
            .select_for_update()
            .get(pk=run_storage.pk)
        )

        manifests = list(
            WorkflowProgressTopologyManifest.objects.using(using)
            .select_for_update()
            .filter(run_storage=run_storage)
            .defer("payload")
            .order_by("topology_version")
        )
        current = next(
            (
                manifest
                for manifest in manifests
                if manifest.slot == WorkflowProgressTopologySlot.CURRENT
            ),
            None,
        )
        pending = next(
            (
                manifest
                for manifest in manifests
                if manifest.slot == WorkflowProgressTopologySlot.PENDING
            ),
            None,
        )
        if current is not None and topology.topology_version <= current.topology_version:
            if topology.topology_version == current.topology_version and (
                _stored_manifest_matches_prepared(current, topology, using=using)
            ):
                return str(current.pk)
            raise WorkflowProgressStorageConflictError(
                "topology version conflicts with the current manifest"
            )
        if pending is not None:
            if _stored_manifest_matches_prepared(pending, topology, using=using):
                return str(pending.pk)
            raise WorkflowProgressStorageConflictError(
                "another topology candidate is already pending"
            )

        stored_pages: list[WorkflowProgressTopologyPage] = []
        for page in topology.pages:
            stored = (
                WorkflowProgressTopologyPage.objects.using(using)
                .filter(run_storage=run_storage, digest=page.digest)
                .defer("payload")
                .first()
            )
            if stored is not None:
                if not _stored_page_matches(
                    stored.pk,
                    page,
                    run_storage_id=run_storage.pk,
                    using=using,
                ):
                    raise WorkflowProgressStorageIntegrityError(
                        "content-addressed topology page conflicts with stored bytes"
                    )
            else:
                try:
                    with transaction.atomic(using=using):
                        stored = WorkflowProgressTopologyPage.objects.using(using).create(
                            run_storage=run_storage,
                            digest=page.digest,
                            collection=page.collection.value,
                            encoding=WorkflowProgressTopologyEncoding.IDENTITY,
                            payload=page.payload,
                            item_count=page.item_count,
                            encoded_bytes=page.encoded_bytes,
                            decoded_bytes=page.decoded_bytes,
                        )
                except DjangoIntegrityError:
                    stored = (
                        WorkflowProgressTopologyPage.objects.using(using)
                        .filter(run_storage=run_storage, digest=page.digest)
                        .defer("payload")
                        .first()
                    )
                    if stored is None or not _stored_page_matches(
                        stored.pk,
                        page,
                        run_storage_id=run_storage.pk,
                        using=using,
                    ):
                        raise WorkflowProgressStorageIntegrityError(
                            "concurrent topology page creation conflicted"
                        ) from None
            stored_pages.append(stored)

        node_page_count = sum(
            page.collection is WorkflowProgressTopologyCollection.NODE for page in topology.pages
        )
        edge_page_count = len(topology.pages) - node_page_count
        try:
            with transaction.atomic(using=using):
                manifest = WorkflowProgressTopologyManifest.objects.using(using).create(
                    run_storage=run_storage,
                    topology_version=topology.topology_version,
                    slot=WorkflowProgressTopologySlot.PENDING,
                    manifest_digest=topology.manifest_digest,
                    truncation_reasons=_encode_truncation_reasons(topology.truncation_reasons),
                    payload=topology.manifest_payload,
                    node_count=topology.retained_node_count,
                    edge_count=topology.retained_edge_count,
                    node_page_count=node_page_count,
                    edge_page_count=edge_page_count,
                    encoded_bytes=topology.encoded_bytes,
                    decoded_bytes=topology.decoded_bytes,
                )
                WorkflowProgressTopologyManifestPage.objects.using(using).bulk_create(
                    [
                        WorkflowProgressTopologyManifestPage(
                            manifest=manifest,
                            page=stored_page,
                            collection=page.collection.value,
                            page_index=page.page_index,
                        )
                        for page, stored_page in zip(
                            topology.pages,
                            stored_pages,
                            strict=True,
                        )
                    ]
                )
        except DjangoIntegrityError as error:
            raise WorkflowProgressStorageConflictError(
                "concurrent topology candidate creation conflicted"
            ) from error
        if not _stored_manifest_matches_prepared(manifest, topology, using=using):
            raise WorkflowProgressStorageIntegrityError(
                "staged topology failed post-write verification"
            )
        if not RayTaskExecution.objects.using(using).filter(**exact_execution).exists():
            transaction.set_rollback(True, using=using)
            return None
        return str(manifest.pk)


def discard_workflow_progress_topology_candidate(
    identity: WorkflowRunIdentity,
    *,
    manifest_id: str | None = None,
    using: str = "default",
) -> bool:
    """Delete one unpublished candidate and only pages no manifest still references."""
    _validate_run_identity(identity)
    normalized_manifest_id = _manifest_uuid(manifest_id) if manifest_id is not None else None
    with transaction.atomic(using=using):
        run_storage = (
            WorkflowProgressRunStorage.objects.using(using)
            .select_for_update()
            .filter(
                execution_id=identity.task_execution_pk,
                attempt_number=identity.attempt_number,
                execution_generation=identity.execution_generation,
                run_id=identity.run_id,
            )
            .first()
        )
        if run_storage is None:
            return False
        pending_query = (
            WorkflowProgressTopologyManifest.objects.using(using)
            .select_for_update()
            .defer("payload")
            .filter(
                run_storage=run_storage,
                slot=WorkflowProgressTopologySlot.PENDING,
            )
        )
        if normalized_manifest_id is not None:
            pending_query = pending_query.filter(pk=normalized_manifest_id)
        pending = pending_query.first()
        if pending is None:
            return False
        pending.delete(using=using)
        WorkflowProgressTopologyPage.objects.using(using).filter(
            run_storage=run_storage,
            manifest_links__isnull=True,
        ).only("pk").delete()
        return True


def _batched[T](values: list[T], size: int) -> Iterable[list[T]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _verify_stored_node_detail_row(
    row: Mapping[str, Any],
    *,
    identity: WorkflowRunIdentity,
    maximum_topology_version: int,
    maximum_detail_revision: int | None,
) -> tuple[bytes, PreparedWorkflowProgressNodeDetail, bool]:
    """Verify one bounded row and its publication epochs."""
    octets = row["_payload_octets"]
    if (
        type(octets) is not int
        or octets <= 0
        or octets > WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES
        or row["_bounded_payload"] is None
    ):
        raise WorkflowProgressStorageIntegrityError("stored workflow node detail is oversized")
    if (
        type(row["last_topology_version"]) is not int
        or not 1 <= row["last_topology_version"] <= maximum_topology_version
        or type(row["last_detail_revision"]) is not int
        or maximum_detail_revision is None
        or not 1 <= row["last_detail_revision"] <= maximum_detail_revision
    ):
        raise WorkflowProgressStorageIntegrityError(
            "stored workflow node detail publication epochs are invalid"
        )
    payload = _as_bytes(row["_bounded_payload"], "stored workflow node detail")
    expected_key = hashlib.sha256(
        _utf8_bytes(row["node_id"], "stored workflow node_id")
    ).hexdigest()
    if (
        len(payload) != octets
        or row["encoded_bytes"] != octets
        or row["decoded_bytes"] != octets
        or row["node_key"] != expected_key
        or _digest(_DETAIL_DOMAIN, payload) != row["digest"]
        or not 0 <= row["event_count"] <= WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS
    ):
        raise WorkflowProgressStorageIntegrityError(
            "stored workflow node detail metadata is invalid"
        )
    value = _decode_canonical_payload(payload, "stored workflow node detail")
    if not isinstance(value, dict):
        raise WorkflowProgressStorageIntegrityError("stored workflow node detail must be an object")
    try:
        authenticated = _prepare_workflow_progress_node_detail(
            value,
            identity=identity,
            allow_stored_truncation=True,
            enforce_current_preview_redaction=False,
            apply_current_text_policy=False,
        )
    except WorkflowProgressStorageError as error:
        raise WorkflowProgressStorageIntegrityError(
            "stored workflow node detail failed protocol validation"
        ) from error
    if (
        authenticated.node_key != row["node_key"]
        or authenticated.node_id != row["node_id"]
        or authenticated.invocation_id != row["invocation_id"]
        or authenticated.state != row["state"]
        or authenticated.truncated != row["truncated"]
        or authenticated.digest != row["digest"]
        or authenticated.encoded_bytes != row["encoded_bytes"]
        or authenticated.decoded_bytes != row["decoded_bytes"]
        or authenticated.event_count != row["event_count"]
        or authenticated.payload != payload
    ):
        raise WorkflowProgressStorageIntegrityError(
            "stored workflow node detail is not normalized or canonically stored"
        )
    try:
        presented = _prepare_workflow_progress_node_detail(
            value,
            identity=identity,
            allow_stored_truncation=True,
            enforce_current_preview_redaction=False,
            allow_current_policy_truncation=True,
        )
    except WorkflowProgressStorageError as error:
        raise WorkflowProgressStorageIntegrityError(
            "stored workflow node detail failed presentation validation"
        ) from error
    if (
        presented.node_key != authenticated.node_key
        or presented.node_id != authenticated.node_id
        or presented.invocation_id != authenticated.invocation_id
        or presented.state != authenticated.state
        or presented.event_count != authenticated.event_count
    ):
        raise WorkflowProgressStorageIntegrityError(
            "stored workflow node detail identity is unsafe"
        )
    return payload, presented, value["fanout"] is not None


def verify_workflow_progress_node_detail_record(
    row: Mapping[str, Any],
    *,
    identity: WorkflowRunIdentity,
    maximum_topology_version: int,
    maximum_detail_revision: int,
) -> dict[str, Any]:
    """Verify and detach one bounded normalized latest-state detail record."""
    _payload, normalized, _has_fanout = _verify_stored_node_detail_row(
        row,
        identity=identity,
        maximum_topology_version=maximum_topology_version,
        maximum_detail_revision=maximum_detail_revision,
    )
    # The stored digest authenticates the original storage-protocol-v1 payload.
    # Return the independently normalized presentation so terminal formatting
    # from an older redaction implementation cannot reach current readers.
    value = _decode_canonical_payload(normalized.payload, "stored workflow node detail")
    if not isinstance(value, dict):
        raise WorkflowProgressStorageIntegrityError("stored workflow node detail must be an object")
    return value


def _verified_touched_node_rows(
    run_storage: WorkflowProgressRunStorage,
    *,
    node_ids: set[str],
    node_keys: set[str],
    identity: WorkflowRunIdentity,
    maximum_topology_version: int | None = None,
    using: str,
) -> dict[str, dict[str, Any]]:
    if not node_ids and not node_keys:
        return {}
    connection = connections[using]
    maximum_in = connection.ops.max_in_list_size() or 1000
    batch_size = max(1, min(maximum_in // 2, 500))
    pairs = sorted(
        (hashlib.sha256(_utf8_bytes(node_id, "workflow node_id")).hexdigest(), node_id)
        for node_id in node_ids
    )
    if {key for key, _ in pairs} != node_keys or len(pairs) != len(node_keys):
        raise WorkflowProgressStorageIntegrityError(
            "workflow node identities do not match their storage keys"
        )
    rows_by_key: dict[str, dict[str, Any]] = {}
    for batch in _batched(pairs, batch_size):
        batch_keys = [key for key, _ in batch]
        batch_ids = [node_id for _, node_id in batch]
        rows = list(
            WorkflowProgressNodeDetail.objects.using(using)
            .select_for_update()
            .filter(run_storage=run_storage)
            .filter(Q(node_key__in=batch_keys) | Q(node_id__in=batch_ids))
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
            .values(
                "pk",
                "node_key",
                "node_id",
                "invocation_id",
                "state",
                "truncated",
                "digest",
                "encoded_bytes",
                "decoded_bytes",
                "event_count",
                "last_topology_version",
                "last_detail_revision",
                "_payload_octets",
                "_bounded_payload",
            )[: len(batch) + 1]
        )
        if len(rows) > len(batch):
            raise WorkflowProgressStorageIntegrityError(
                "stored workflow node detail identities are duplicated"
            )
        exact_ids = set(batch_ids)
        exact_keys = set(batch_keys)
        for row in rows:
            if row["node_key"] not in exact_keys and row["node_id"] not in exact_ids:
                continue
            payload, normalized, _ = _verify_stored_node_detail_row(
                row,
                identity=identity,
                maximum_topology_version=(
                    maximum_topology_version
                    if maximum_topology_version is not None
                    else WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER
                ),
                maximum_detail_revision=run_storage.detail_revision,
            )
            if row["node_key"] in rows_by_key:
                raise WorkflowProgressStorageIntegrityError(
                    "stored workflow node detail identity is duplicated"
                )
            row["payload"] = payload
            row["prepared"] = normalized
            rows_by_key[row["node_key"]] = row
    for row in rows_by_key.values():
        if row["node_id"] in node_ids and row["node_key"] not in node_keys:
            raise WorkflowProgressStorageIntegrityError(
                "stored workflow node key collides with an exact node identity"
            )
    return rows_by_key


def _rebalance_sparse_recent_events(
    prepared_records: list[PreparedWorkflowProgressNodeDetail],
    old_rows: Mapping[str, Mapping[str, Any]],
    *,
    removal_ids: set[str],
) -> list[PreparedWorkflowProgressNodeDetail]:
    """Apply the same deterministic run-global event cap to a sparse update."""
    records_by_id = {record.node_id: record for record in prepared_records}
    for row in old_rows.values():
        node_id = row["node_id"]
        if row["event_count"] and node_id not in records_by_id and node_id not in removal_ids:
            records_by_id[node_id] = row["prepared"]

    decoded_records: dict[str, dict[str, Any]] = {}
    event_entries: list[tuple[tuple[Any, ...], str, dict[str, Any]]] = []
    for node_id, record in records_by_id.items():
        decoded = _decode_canonical_payload(record.payload, "prepared node detail")
        if not isinstance(decoded, dict):
            raise WorkflowProgressStorageError("prepared node detail must be an object")
        decoded_records[node_id] = decoded
        for occurrence, event in enumerate(decoded["recent_events"]):
            event_entries.append(
                (
                    _event_sort_key(event, node_id=node_id, occurrence=occurrence),
                    node_id,
                    event,
                )
            )
    selected_events = sorted(event_entries, key=lambda item: item[0])[
        -WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS:
    ]
    events_by_node: dict[str, list[dict[str, Any]]] = {}
    for _, node_id, event in selected_events:
        events_by_node.setdefault(node_id, []).append(event)

    rebalanced: list[PreparedWorkflowProgressNodeDetail] = []
    for node_id, record in records_by_id.items():
        retained_events = sorted(events_by_node.get(node_id, []), key=_event_sort_key)
        decoded = decoded_records[node_id]
        if retained_events != decoded["recent_events"]:
            decoded["recent_events"] = retained_events
            record = _prepared_node_detail(
                decoded,
                invocation_id=record.invocation_id,
                truncated=True,
            )
        rebalanced.append(record)
    return sorted(rebalanced, key=lambda record: record.node_key)


def _storage_bound_summary(
    summary: Mapping[str, Any],
    *,
    identity: WorkflowRunIdentity,
    topology: VerifiedWorkflowProgressTopology,
    detail_revision: int,
    detail_node_count: int,
    detail_state_counts: Mapping[str, int],
    detail_truncated_count: int,
    storage_reasons: set[str],
    observed_node_count: int | None = None,
    observed_edge_count: int | None = None,
    observed_detail_count: int | None = None,
) -> tuple[dict[str, Any], str]:
    if not isinstance(summary, Mapping):
        raise WorkflowProgressStorageError("workflow progress summary must be an object")
    candidate = deepcopy(dict(summary))
    try:
        node_counts = dict(candidate["node_counts"])
        edge_counts = dict(candidate["edge_counts"])
        detail = dict(candidate["detail"])
        retention = dict(candidate["retention"])
        storage = dict(candidate["storage"])
    except (KeyError, TypeError, ValueError) as error:
        raise WorkflowProgressStorageError(
            "workflow progress summary is missing storage-owned fields"
        ) from error
    if candidate.get("reporting_policy") == "disabled" or detail.get("availability") in {
        WorkflowProgressDetailAvailability.OMITTED_BY_POLICY.value,
        WorkflowProgressDetailAvailability.DISABLED.value,
    }:
        raise WorkflowProgressStorageConflictError(
            "summary-only workflow progress cannot publish detail storage"
        )
    discovered_nodes = node_counts.get("discovered")
    discovered_edges = edge_counts.get("discovered")
    if type(discovered_nodes) is not int or type(discovered_edges) is not int:
        raise WorkflowProgressStorageError("workflow progress discovered counts must be integers")
    if (
        observed_node_count is not None
        and discovered_nodes != observed_node_count
        or observed_edge_count is not None
        and discovered_edges != observed_edge_count
        or observed_detail_count is not None
        and discovered_nodes != observed_detail_count
    ):
        raise WorkflowProgressStorageConflictError(
            "workflow summary discovered counts conflict with prepared evidence"
        )
    detail_days = get_settings()["WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS"]
    if type(detail_days) is not int or not 0 <= detail_days <= 30:
        raise WorkflowProgressStorageError(
            "workflow progress detail retention must be an integer from 0 through 30"
        )
    reasons_value = detail.get("truncation_reasons")
    if not isinstance(reasons_value, list) or any(
        not isinstance(reason, str) for reason in reasons_value
    ):
        raise WorkflowProgressStorageError(
            "workflow progress truncation reasons must be a list of strings"
        )
    reasons = set(storage_reasons)
    if detail_truncated_count:
        reasons.add(WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
    topology_reasons = {
        WorkflowProgressTruncationReason.TOPOLOGY_ENCODED_BYTES.value,
        WorkflowProgressTruncationReason.TOPOLOGY_DECODED_BYTES.value,
        WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value,
    }
    node_reasons = topology_reasons | {WorkflowProgressTruncationReason.NODE_COUNT_LIMIT.value}
    edge_reasons = node_reasons | {WorkflowProgressTruncationReason.EDGE_COUNT_LIMIT.value}
    detail_reasons = {
        WorkflowProgressTruncationReason.DETAIL_COUNT_LIMIT.value,
        WorkflowProgressTruncationReason.DETAIL_ENCODED_BYTES.value,
        WorkflowProgressTruncationReason.DETAIL_DECODED_BYTES.value,
        WorkflowProgressTruncationReason.REPORTING_POLICY.value,
        WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value,
    }
    if topology.node_count < discovered_nodes and reasons.isdisjoint(node_reasons):
        reasons.add(WorkflowProgressTruncationReason.NODE_COUNT_LIMIT.value)
    if topology.edge_count < discovered_edges and reasons.isdisjoint(edge_reasons):
        reasons.add(WorkflowProgressTruncationReason.EDGE_COUNT_LIMIT.value)
    if detail_node_count < topology.node_count and reasons.isdisjoint(detail_reasons):
        reasons.add(WorkflowProgressTruncationReason.DETAIL_COUNT_LIMIT.value)
    complete = (
        not reasons
        and topology.node_count == discovered_nodes
        and topology.edge_count == discovered_edges
        and detail_node_count == discovered_nodes
    )
    observed_state_counts = {
        state: node_counts[state.lower()] for state in _DETAIL_STATE_AGGREGATE_FIELDS
    }
    if any(type(value) is not int or value < 0 for value in observed_state_counts.values()):
        raise WorkflowProgressStorageError(
            "workflow progress node state counts must be non-negative integers"
        )
    if any(
        type(detail_state_counts.get(state)) is not int
        or detail_state_counts[state] < 0
        or detail_state_counts[state] > observed_state_counts[state]
        for state in _DETAIL_STATE_AGGREGATE_FIELDS
    ):
        raise WorkflowProgressStorageConflictError(
            "retained workflow node states conflict with summary aggregates"
        )
    if complete and any(
        detail_state_counts[state] != observed_state_counts[state]
        for state in _DETAIL_STATE_AGGREGATE_FIELDS
    ):
        raise WorkflowProgressStorageConflictError(
            "complete workflow detail does not match summary node states"
        )
    node_counts["retained_topology"] = topology.node_count
    node_counts["retained_detail"] = detail_node_count
    edge_counts["retained_topology"] = topology.edge_count
    detail["availability"] = (
        WorkflowProgressDetailAvailability.AVAILABLE.value
        if complete
        else WorkflowProgressDetailAvailability.TRUNCATED.value
    )
    detail["complete"] = complete
    detail["truncation_reasons"] = sorted(reasons)
    storage["kind"] = "database"
    storage["manifest_id"] = topology.manifest_id
    candidate["topology_version"] = topology.topology_version
    candidate["detail_revision"] = detail_revision
    candidate["node_counts"] = node_counts
    candidate["edge_counts"] = edge_counts
    candidate["detail"] = detail
    retention["detail_days"] = detail_days
    if candidate.get("state") in WORKFLOW_PROGRESS_TERMINAL_STATES:
        try:
            terminal_finished_at = candidate["terminal"]["finished_at"]
            finished = datetime.fromisoformat(terminal_finished_at[:-1] + "+00:00")
        except (KeyError, TypeError, ValueError) as error:
            raise WorkflowProgressStorageConflictError(
                "terminal workflow summary has invalid completion metadata"
            ) from error
        retention["detail_expires_at"] = (
            (finished + timedelta(days=detail_days)).isoformat().replace("+00:00", "Z")
        )
    else:
        retention["detail_expires_at"] = None
    candidate["retention"] = retention
    candidate["storage"] = storage
    try:
        serialized = serialize_workflow_progress_summary(
            candidate,
            expected_identity=identity,
        )
    except WorkflowProgressSummaryError as error:
        raise WorkflowProgressStorageConflictError(
            "workflow progress summary conflicts with stored detail"
        ) from error
    return json.loads(serialized), serialized


def persist_workflow_progress_publication(
    identity: WorkflowRunIdentity,
    summary: Mapping[str, Any],
    *,
    manifest_id: str,
    prepared_topology: PreparedWorkflowProgressTopology | None = None,
    prepared_detail: PreparedWorkflowProgressDetail | None = None,
    detail_records: Iterable[PreparedWorkflowProgressNodeDetail] = (),
    remove_node_ids: Iterable[str] = (),
    using: str = "default",
) -> WorkflowProgressPublicationResult:
    """Atomically publish topology, sparse latest-state detail, and summary pointer."""
    _validate_run_identity(identity)
    manifest_id = _manifest_uuid(manifest_id)
    if prepared_topology is not None:
        _validate_prepared_topology_reference(prepared_topology)
        if prepared_topology.identity != identity:
            raise WorkflowProgressStorageError(
                "prepared topology identity does not match the publication run"
            )
    detail_record_iterator = iter(detail_records)
    if prepared_detail is not None:
        _verify_prepared_detail(prepared_detail, identity=identity)
        if next(detail_record_iterator, None) is not None:
            raise WorkflowProgressStorageError(
                "prepared detail and sparse detail records are mutually exclusive"
            )
        prepared_record_values = list(prepared_detail.records)
    else:
        prepared_record_values = list(
            islice(detail_record_iterator, WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS + 1)
        )
    if len(prepared_record_values) > WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS:
        raise WorkflowProgressStorageLimitError(
            "detail publication exceeds the retained-node limit"
        )
    prepared_records = sorted(prepared_record_values, key=lambda record: record.node_key)
    records_by_key: dict[str, PreparedWorkflowProgressNodeDetail] = {}
    for record in prepared_records:
        _verify_prepared_node_detail(record, identity=identity)
        if record.node_key in records_by_key:
            raise WorkflowProgressStorageError("detail publication contains duplicate nodes")
        records_by_key[record.node_key] = record
    removal_values = list(islice(iter(remove_node_ids), WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS + 1))
    if len(removal_values) > WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS:
        raise WorkflowProgressStorageLimitError(
            "detail publication removals exceed the retained-node limit"
        )
    explicit_removals: set[str] = set()
    for value in removal_values:
        node_id = _bounded_identity_text(
            value,
            "removed workflow node_id",
            max_bytes=WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
        )
        if node_id is None:
            raise AssertionError("non-null removed node identity normalized to None")
        if node_id in explicit_removals:
            raise WorkflowProgressStorageError("detail publication repeats a removed node")
        explicit_removals.add(node_id)
    if explicit_removals & {record.node_id for record in prepared_records}:
        raise WorkflowProgressStorageError(
            "detail publication cannot update and remove the same node"
        )

    with transaction.atomic(using=using):
        execution = (
            RayTaskExecution.objects.using(using)
            .select_for_update()
            .only(
                "pk",
                "state",
                "attempt_number",
                "execution_generation",
                "workflow_run_id",
                "workflow_plan_fingerprint",
                "workflow_plan_selection",
                "workflow_progress_summary_json",
            )
            .filter(
                pk=identity.task_execution_pk,
                state=TaskState.RUNNING,
                attempt_number=identity.attempt_number,
                execution_generation=identity.execution_generation,
                workflow_run_id=identity.run_id,
            )
            .first()
        )
        if execution is None:
            return WorkflowProgressPublicationResult(accepted=False)
        run_storage = (
            WorkflowProgressRunStorage.objects.using(using)
            .select_for_update()
            .filter(
                execution=execution,
                attempt_number=identity.attempt_number,
                execution_generation=identity.execution_generation,
                run_id=identity.run_id,
            )
            .first()
        )
        if run_storage is None:
            raise WorkflowProgressStorageIntegrityError("workflow progress run storage is missing")
        manifests = list(
            WorkflowProgressTopologyManifest.objects.using(using)
            .select_for_update()
            .filter(run_storage=run_storage)
            .defer("payload")
        )
        target_manifest = next(
            (manifest for manifest in manifests if str(manifest.pk) == manifest_id),
            None,
        )
        if target_manifest is None:
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology publication target is missing"
            )
        current_manifest = next(
            (
                manifest
                for manifest in manifests
                if manifest.slot == WorkflowProgressTopologySlot.CURRENT
            ),
            None,
        )
        if (
            target_manifest.slot == WorkflowProgressTopologySlot.CURRENT
            and prepared_topology is not None
        ):
            target = _trusted_current_topology(
                target_manifest,
                prepared_topology,
                run_storage_id=run_storage.pk,
            )
        else:
            target = verify_workflow_progress_topology_manifest(
                manifest_id,
                expected_identity=identity,
                using=using,
            )
            if prepared_topology is not None and (
                target.topology_version != prepared_topology.topology_version
                or target.node_ids != prepared_topology.node_ids
                or target.node_kinds != prepared_topology.node_kinds
                or target.edges != prepared_topology.edges
                or target.encoded_bytes != prepared_topology.encoded_bytes
                or target.decoded_bytes != prepared_topology.decoded_bytes
                or target.truncation_reasons != prepared_topology.truncation_reasons
            ):
                raise WorkflowProgressStorageIntegrityError(
                    "staged topology conflicts with prepared immutable evidence"
                )
        if target.run_storage_id != run_storage.pk:
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology target belongs to another run storage"
            )
        current: VerifiedWorkflowProgressTopology | None = None
        if current_manifest is not None:
            if current_manifest.pk == target_manifest.pk:
                current = target
            else:
                current = verify_workflow_progress_topology_manifest(
                    str(current_manifest.pk),
                    expected_identity=identity,
                    using=using,
                )
        if target.slot == WorkflowProgressTopologySlot.PENDING:
            if current is not None and target.topology_version <= current.topology_version:
                raise WorkflowProgressStorageConflictError(
                    "pending topology does not advance the current version"
                )
        elif target.slot != WorkflowProgressTopologySlot.CURRENT:
            raise WorkflowProgressStorageIntegrityError(
                "workflow topology target has an invalid slot"
            )

        for record in prepared_records:
            if record.node_id not in target.node_ids:
                raise WorkflowProgressStorageConflictError(
                    "node detail is not present in the publication topology"
                )
            decoded = _decode_canonical_payload(record.payload, "prepared node detail")
            has_fanout = isinstance(decoded, dict) and decoded.get("fanout") is not None
            if (record.node_id in target.map_node_ids) != has_fanout:
                raise WorkflowProgressStorageConflictError(
                    "node detail fanout conflicts with the publication topology"
                )
        if any(node_id not in target.node_ids for node_id in explicit_removals):
            raise WorkflowProgressStorageConflictError(
                "explicit detail removal is not present in the publication topology"
            )
        topology_removals: set[str] = set()
        if current is not None and current.manifest_id != target.manifest_id:
            topology_removals.update(current.node_ids - target.node_ids)
            current_kinds = dict(current.node_kinds)
            target_kinds = dict(target.node_kinds)
            supplied_node_ids = {record.node_id for record in prepared_records}
            topology_removals.update(
                node_id
                for node_id in current.node_ids & target.node_ids
                if current_kinds[node_id] != target_kinds[node_id]
                and node_id not in supplied_node_ids
            )
        removal_ids = topology_removals | explicit_removals
        event_node_ids = list(
            WorkflowProgressNodeDetail.objects.using(using)
            .select_for_update()
            .filter(run_storage=run_storage, event_count__gt=0)
            .order_by("node_key")
            .values_list("node_id", flat=True)[: WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS + 1]
        )
        if len(event_node_ids) > WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS:
            raise WorkflowProgressStorageIntegrityError(
                "stored workflow detail exceeds the run-global event-row bound"
            )
        touched_ids = {record.node_id for record in prepared_records} | removal_ids
        touched_ids.update(event_node_ids)
        touched_keys = {
            hashlib.sha256(_utf8_bytes(node_id, "workflow node_id")).hexdigest()
            for node_id in touched_ids
        }
        old_rows = _verified_touched_node_rows(
            run_storage,
            node_ids=touched_ids,
            node_keys=touched_keys,
            identity=identity,
            maximum_topology_version=target.topology_version,
            using=using,
        )

        detail_count = run_storage.detail_node_count
        detail_encoded = run_storage.detail_encoded_bytes
        detail_decoded = run_storage.detail_decoded_bytes
        detail_events = run_storage.detail_event_count
        detail_state_counts = {
            state: getattr(run_storage, field)
            for state, field in _DETAIL_STATE_AGGREGATE_FIELDS.items()
        }
        detail_truncated = run_storage.detail_truncated_count
        detail_storage_reasons = set(
            _decode_truncation_reasons(
                run_storage.detail_truncation_reasons,
                stored=True,
            )
        )
        if prepared_detail is not None:
            if run_storage.detail_revision is not None:
                raise WorkflowProgressStorageConflictError(
                    "prepared aggregate detail is only valid for an initial publication"
                )
            evidence_reasons = set(prepared_detail.truncation_reasons)
            if not set(target.truncation_reasons).issubset(evidence_reasons):
                raise WorkflowProgressStorageError(
                    "prepared detail omits topology truncation evidence"
                )
            detail_storage_reasons.update(evidence_reasons & _DETAIL_TRUNCATION_REASONS)
        if (
            not 0 <= detail_count <= WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS
            or not 0 <= detail_encoded <= WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES
            or not 0 <= detail_decoded <= WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES
            or not 0 <= detail_events <= WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS
            or any(
                type(count) is not int or not 0 <= count <= WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS
                for count in detail_state_counts.values()
            )
            or sum(detail_state_counts.values()) != detail_count
            or type(detail_truncated) is not int
            or not 0 <= detail_truncated <= detail_count
            or not detail_storage_reasons.issubset(_DETAIL_TRUNCATION_REASONS)
            or target.encoded_bytes + detail_encoded > WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES
            or target.decoded_bytes + detail_decoded > WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES
        ):
            raise WorkflowProgressStorageIntegrityError(
                "workflow detail aggregates violate storage limits"
            )
        delete_ids: set[int] = set()
        for node_id in sorted(removal_ids):
            node_key = hashlib.sha256(_utf8_bytes(node_id, "workflow node_id")).hexdigest()
            old = old_rows.get(node_key)
            if old is None:
                continue
            if old["node_id"] != node_id:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow node hash collision detected during removal"
                )
            delete_ids.add(old["pk"])
            detail_count -= 1
            detail_encoded -= old["encoded_bytes"]
            detail_decoded -= old["decoded_bytes"]
            detail_events -= old["event_count"]
            detail_state_counts[old["state"]] -= 1
            detail_truncated -= int(old["truncated"])

        now = timezone.now()
        storage_reasons = set(target.truncation_reasons) | detail_storage_reasons
        pending_changes: list[tuple[PreparedWorkflowProgressNodeDetail, dict[str, Any] | None]] = []
        for record in prepared_records:
            old = old_rows.get(record.node_key)
            if old is not None and old["node_id"] != record.node_id:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow node hash collision detected during publication"
                )
            if old is not None and (
                old["payload"] == record.payload
                and old["digest"] == record.digest
                and old["state"] == record.state
                and old["truncated"] == record.truncated
                and old["invocation_id"] == record.invocation_id
                and old["event_count"] == record.event_count
            ):
                continue
            pending_changes.append((record, old))

        provisional_changes: list[
            tuple[PreparedWorkflowProgressNodeDetail, dict[str, Any] | None]
        ] = []
        rejected_node_ids: set[str] = set()
        for record, old in pending_changes:
            old_count = 1 if old is not None else 0
            old_encoded = old["encoded_bytes"] if old is not None else 0
            old_decoded = old["decoded_bytes"] if old is not None else 0
            old_events = old["event_count"] if old is not None else 0
            candidate_count = detail_count - old_count + 1
            candidate_encoded = detail_encoded - old_encoded + record.encoded_bytes
            candidate_decoded = detail_decoded - old_decoded + record.decoded_bytes
            candidate_events = detail_events - old_events + record.event_count
            candidate_state_counts = dict(detail_state_counts)
            if old is not None:
                candidate_state_counts[old["state"]] -= 1
            candidate_state_counts[record.state] += 1
            candidate_truncated = (
                detail_truncated
                - int(old["truncated"] if old is not None else False)
                + int(record.truncated)
            )
            reason: str | None = None
            if candidate_count > WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS:
                reason = WorkflowProgressTruncationReason.DETAIL_COUNT_LIMIT.value
            elif (
                candidate_encoded > WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES
                or target.encoded_bytes + candidate_encoded
                > WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES
            ):
                reason = WorkflowProgressTruncationReason.DETAIL_ENCODED_BYTES.value
            elif (
                candidate_decoded > WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES
                or target.decoded_bytes + candidate_decoded
                > WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES
            ):
                reason = WorkflowProgressTruncationReason.DETAIL_DECODED_BYTES.value
            if reason is not None:
                storage_reasons.add(reason)
                detail_storage_reasons.add(reason)
                rejected_node_ids.add(record.node_id)
                if old is not None and old["pk"] not in delete_ids:
                    delete_ids.add(old["pk"])
                    detail_count -= 1
                    detail_encoded -= old_encoded
                    detail_decoded -= old_decoded
                    detail_events -= old_events
                    detail_state_counts[old["state"]] -= 1
                    detail_truncated -= int(old["truncated"])
                continue
            detail_count = candidate_count
            detail_encoded = candidate_encoded
            detail_decoded = candidate_decoded
            detail_events = candidate_events
            detail_state_counts = candidate_state_counts
            detail_truncated = candidate_truncated
            provisional_changes.append((record, old))

        deleted_node_ids = {
            str(row["node_id"]) for row in old_rows.values() if row["pk"] in delete_ids
        }
        rebalanced_records = _rebalance_sparse_recent_events(
            [record for record, _ in provisional_changes],
            old_rows,
            removal_ids=removal_ids | rejected_node_ids | deleted_node_ids,
        )
        final_by_key: dict[str, PreparedWorkflowProgressNodeDetail] = {}
        for record in rebalanced_records:
            _verify_prepared_node_detail(record, identity=identity)
            if record.node_key in final_by_key:
                raise WorkflowProgressStorageIntegrityError(
                    "sparse event rebalancing duplicated a workflow node"
                )
            if record.node_id not in target.node_ids:
                raise WorkflowProgressStorageConflictError(
                    "rebalanced node detail is not present in the publication topology"
                )
            decoded = _decode_canonical_payload(record.payload, "prepared node detail")
            has_fanout = isinstance(decoded, dict) and decoded.get("fanout") is not None
            if (record.node_id in target.map_node_ids) != has_fanout:
                raise WorkflowProgressStorageConflictError(
                    "rebalanced node detail fanout conflicts with the publication topology"
                )
            final_by_key[record.node_key] = record

        provisional_by_key = {record.node_key: (record, old) for record, old in provisional_changes}
        if not provisional_by_key.keys() <= final_by_key.keys():
            raise WorkflowProgressStorageIntegrityError(
                "sparse event rebalancing omitted an admitted workflow node"
            )

        accepted_changes: list[
            tuple[PreparedWorkflowProgressNodeDetail, dict[str, Any] | None]
        ] = []
        for node_key, record in final_by_key.items():
            provisional = provisional_by_key.get(node_key)
            old = old_rows.get(node_key)
            if provisional is not None:
                source, expected_old = provisional
                if expected_old is not old:
                    raise WorkflowProgressStorageIntegrityError(
                        "sparse event rebalancing changed workflow row ownership"
                    )
                detail_encoded += record.encoded_bytes - source.encoded_bytes
                detail_decoded += record.decoded_bytes - source.decoded_bytes
                detail_events += record.event_count - source.event_count
                detail_truncated += int(record.truncated) - int(source.truncated)
                if (
                    record.state != source.state
                    or record.invocation_id != source.invocation_id
                    or record.node_id != source.node_id
                ):
                    raise WorkflowProgressStorageIntegrityError(
                        "sparse event rebalancing changed non-event detail"
                    )
                if record.event_count < source.event_count:
                    detail_storage_reasons.add(
                        WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value
                    )
            else:
                if old is None or not old["event_count"] or old["pk"] in delete_ids:
                    raise WorkflowProgressStorageIntegrityError(
                        "sparse event rebalancing introduced an unexpected workflow node"
                    )
                detail_encoded += record.encoded_bytes - old["encoded_bytes"]
                detail_decoded += record.decoded_bytes - old["decoded_bytes"]
                detail_events += record.event_count - old["event_count"]
                detail_truncated += int(record.truncated) - int(old["truncated"])
                if (
                    record.state != old["state"]
                    or record.invocation_id != old["invocation_id"]
                    or record.node_id != old["node_id"]
                ):
                    raise WorkflowProgressStorageIntegrityError(
                        "sparse event rebalancing changed stored non-event detail"
                    )
                if record.event_count < old["event_count"]:
                    detail_storage_reasons.add(
                        WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value
                    )
            if old is not None and (
                old["payload"] == record.payload
                and old["digest"] == record.digest
                and old["state"] == record.state
                and old["truncated"] == record.truncated
                and old["invocation_id"] == record.invocation_id
                and old["event_count"] == record.event_count
            ):
                continue
            accepted_changes.append((record, old))

        storage_reasons = set(target.truncation_reasons) | detail_storage_reasons

        current_detail_revision = run_storage.detail_revision
        has_logical_change = bool(delete_ids or accepted_changes) or current_detail_revision is None
        if has_logical_change:
            if current_detail_revision is None:
                detail_revision = 1
            elif current_detail_revision >= WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER:
                raise WorkflowProgressStorageConflictError(
                    "workflow detail revision cannot advance"
                )
            else:
                detail_revision = current_detail_revision + 1
        else:
            detail_revision = current_detail_revision
        if detail_revision is None:
            raise AssertionError("published detail revision cannot be None")

        to_create: list[WorkflowProgressNodeDetail] = []
        to_update: list[WorkflowProgressNodeDetail] = []
        for record, old in accepted_changes:
            values = {
                "run_storage": run_storage,
                "node_key": record.node_key,
                "node_id": record.node_id,
                "invocation_id": record.invocation_id,
                "state": record.state,
                "truncated": record.truncated,
                "payload": record.payload,
                "digest": record.digest,
                "encoded_bytes": record.encoded_bytes,
                "decoded_bytes": record.decoded_bytes,
                "event_count": record.event_count,
                "last_topology_version": target.topology_version,
                "last_detail_revision": detail_revision,
                "updated_at": now,
            }
            if old is None:
                to_create.append(WorkflowProgressNodeDetail(**values))
            else:
                to_update.append(WorkflowProgressNodeDetail(pk=old["pk"], **values))

        if (
            min(
                detail_count,
                detail_encoded,
                detail_decoded,
                detail_events,
                detail_truncated,
                *detail_state_counts.values(),
            )
            < 0
            or detail_count > WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS
            or detail_encoded > WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES
            or detail_decoded > WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES
            or detail_events > WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS
            or target.encoded_bytes + detail_encoded > WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES
            or target.decoded_bytes + detail_decoded > WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES
            or sum(detail_state_counts.values()) != detail_count
            or detail_truncated > detail_count
        ):
            raise WorkflowProgressStorageIntegrityError(
                "workflow detail aggregate delta underflowed"
            )
        if detail_count == target.node_count:
            detail_storage_reasons.clear()
            if summary.get("reporting_policy") != "full":
                detail_storage_reasons.add(WorkflowProgressTruncationReason.REPORTING_POLICY.value)
            if detail_truncated:
                detail_storage_reasons.add(WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
        elif detail_storage_reasons.isdisjoint(_DETAIL_TRUNCATION_REASONS):
            detail_storage_reasons.add(WorkflowProgressTruncationReason.DETAIL_COUNT_LIMIT.value)
        storage_reasons = set(target.truncation_reasons) | detail_storage_reasons
        normalized_summary, serialized_summary = _storage_bound_summary(
            summary,
            identity=identity,
            topology=target,
            detail_revision=detail_revision,
            detail_node_count=detail_count,
            detail_state_counts=detail_state_counts,
            detail_truncated_count=detail_truncated,
            storage_reasons=storage_reasons,
            observed_node_count=(
                prepared_topology.observed_node_count if prepared_topology is not None else None
            ),
            observed_edge_count=(
                prepared_topology.observed_edge_count if prepared_topology is not None else None
            ),
            observed_detail_count=(
                prepared_detail.observed_count if prepared_detail is not None else None
            ),
        )
        if delete_ids:
            WorkflowProgressNodeDetail.objects.using(using).filter(pk__in=delete_ids).delete()
        if to_update:
            WorkflowProgressNodeDetail.objects.using(using).bulk_update(
                to_update,
                [
                    "invocation_id",
                    "state",
                    "truncated",
                    "payload",
                    "digest",
                    "encoded_bytes",
                    "decoded_bytes",
                    "event_count",
                    "last_topology_version",
                    "last_detail_revision",
                    "updated_at",
                ],
            )
        if to_create:
            WorkflowProgressNodeDetail.objects.using(using).bulk_create(to_create)

        run_storage.detail_revision = detail_revision
        run_storage.detail_node_count = detail_count
        run_storage.detail_encoded_bytes = detail_encoded
        run_storage.detail_decoded_bytes = detail_decoded
        run_storage.detail_event_count = detail_events
        for state, field in _DETAIL_STATE_AGGREGATE_FIELDS.items():
            setattr(run_storage, field, detail_state_counts[state])
        run_storage.detail_truncated_count = detail_truncated
        run_storage.detail_truncation_reasons = _encode_truncation_reasons(detail_storage_reasons)
        run_storage.detail_retention_days = normalized_summary["retention"]["detail_days"]
        expires_at = normalized_summary["retention"]["detail_expires_at"]
        run_storage.detail_expires_at = (
            datetime.fromisoformat(expires_at[:-1] + "+00:00")
            if isinstance(expires_at, str)
            else None
        )
        run_storage.updated_at = now
        run_storage.cleanup_error = None
        run_storage.save(
            update_fields=[
                "detail_revision",
                "detail_node_count",
                "detail_encoded_bytes",
                "detail_decoded_bytes",
                "detail_event_count",
                *_DETAIL_STATE_AGGREGATE_FIELDS.values(),
                "detail_truncated_count",
                "detail_truncation_reasons",
                "detail_retention_days",
                "detail_expires_at",
                "updated_at",
                "cleanup_error",
            ]
        )

        if target_manifest.slot == WorkflowProgressTopologySlot.PENDING:
            if current_manifest is not None:
                current_manifest.delete(using=using)
            target_manifest.slot = WorkflowProgressTopologySlot.CURRENT
            target_manifest.published_at = now
            target_manifest.cleanup_error = None
            target_manifest.save(update_fields=["slot", "published_at", "cleanup_error"])
        from django_ray.workflow.progress.runs import _assign_workflow_progress_summary_locked

        if not _assign_workflow_progress_summary_locked(
            execution,
            identity,
            serialized_summary,
        ):
            raise WorkflowProgressStorageConflictError(
                "workflow run lost ownership during atomic publication"
            )
        WorkflowProgressTopologyPage.objects.using(using).filter(
            run_storage=run_storage,
            manifest_links__isnull=True,
        ).only("pk").delete()
        return WorkflowProgressPublicationResult(
            accepted=True,
            summary=normalized_summary,
            changed_node_count=len(to_create) + len(to_update),
            removed_node_count=len(delete_ids),
        )


def audit_workflow_progress_detail_storage(
    identity: WorkflowRunIdentity,
    *,
    using: str = "default",
) -> WorkflowProgressDetailAuditResult:
    """Read-only, whole-run verification of normalized latest-state detail.

    This intentionally expensive path is for periodic or operator-initiated
    integrity checks.  Sparse publications continue to verify only touched
    rows.  The task and exact run are locked in publication order so the audit
    observes one stable current topology and detail revision.
    """
    _validate_run_identity(identity)
    with transaction.atomic(using=using):
        execution = (
            RayTaskExecution.objects.using(using)
            .select_for_update()
            .only(
                "pk",
                "attempt_number",
                "execution_generation",
                "workflow_run_id",
                "workflow_progress_summary_json",
            )
            .filter(pk=identity.task_execution_pk)
            .first()
        )
        if execution is None:
            raise WorkflowProgressStorageIntegrityError("workflow progress audit task is missing")
        run_storage = (
            WorkflowProgressRunStorage.objects.using(using)
            .select_for_update()
            .filter(
                execution_id=execution.pk,
                attempt_number=identity.attempt_number,
                execution_generation=identity.execution_generation,
                run_id=identity.run_id,
            )
            .first()
        )
        if run_storage is None:
            raise WorkflowProgressStorageIntegrityError("workflow progress audit run is missing")

        detail_revision = run_storage.detail_revision
        state_counts = {
            state: getattr(run_storage, field)
            for state, field in _DETAIL_STATE_AGGREGATE_FIELDS.items()
        }
        aggregate_values = (
            run_storage.detail_node_count,
            run_storage.detail_encoded_bytes,
            run_storage.detail_decoded_bytes,
            run_storage.detail_event_count,
            run_storage.detail_truncated_count,
            *state_counts.values(),
        )
        detail_reasons = set(
            _decode_truncation_reasons(
                run_storage.detail_truncation_reasons,
                stored=True,
            )
        )
        if (
            (detail_revision is not None and type(detail_revision) is not int)
            or (detail_revision is not None and detail_revision <= 0)
            or any(type(value) is not int or value < 0 for value in aggregate_values)
            or run_storage.detail_node_count > WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS
            or run_storage.detail_encoded_bytes > WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES
            or run_storage.detail_decoded_bytes > WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES
            or run_storage.detail_encoded_bytes != run_storage.detail_decoded_bytes
            or run_storage.detail_event_count > WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS
            or any(count > WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS for count in state_counts.values())
            or sum(state_counts.values()) != run_storage.detail_node_count
            or run_storage.detail_truncated_count > run_storage.detail_node_count
            or not detail_reasons.issubset(_DETAIL_TRUNCATION_REASONS)
            or type(run_storage.detail_retention_days) is not int
            or not 0 <= run_storage.detail_retention_days <= 30
            or (detail_revision is None and (any(aggregate_values) or bool(detail_reasons)))
        ):
            raise WorkflowProgressStorageIntegrityError(
                "workflow progress audit run aggregates are invalid"
            )

        current_manifest_ids = list(
            WorkflowProgressTopologyManifest.objects.using(using)
            .filter(
                run_storage=run_storage,
                slot=WorkflowProgressTopologySlot.CURRENT,
            )
            .order_by("pk")
            .values_list("pk", flat=True)[:2]
        )
        if len(current_manifest_ids) > 1:
            raise WorkflowProgressStorageIntegrityError(
                "workflow progress audit found multiple current topologies"
            )
        topology: VerifiedWorkflowProgressTopology | None = None
        if current_manifest_ids:
            topology = verify_workflow_progress_topology_manifest(
                str(current_manifest_ids[0]),
                expected_identity=identity,
                using=using,
            )
            if topology.run_storage_id != run_storage.pk:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit topology belongs to another run"
                )
            if detail_revision is None:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit current topology has no detail revision"
                )
            if (
                topology.encoded_bytes + run_storage.detail_encoded_bytes
                > WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES
                or topology.decoded_bytes + run_storage.detail_decoded_bytes
                > WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES
            ):
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit combined aggregates exceed storage limits"
                )
        elif detail_revision is not None:
            raise WorkflowProgressStorageIntegrityError(
                "workflow progress audit detail has no current topology"
            )

        row_query = (
            WorkflowProgressNodeDetail.objects.using(using)
            .filter(run_storage=run_storage)
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
            .order_by("node_key", "pk")
            .values(
                "pk",
                "node_key",
                "node_id",
                "invocation_id",
                "state",
                "truncated",
                "digest",
                "encoded_bytes",
                "decoded_bytes",
                "event_count",
                "last_topology_version",
                "last_detail_revision",
                "_payload_octets",
                "_bounded_payload",
            )[: WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS + 1]
        )
        actual_node_count = 0
        actual_encoded_bytes = 0
        actual_decoded_bytes = 0
        actual_event_count = 0
        actual_truncated_count = 0
        actual_state_counts = dict.fromkeys(_DETAIL_STATE_AGGREGATE_FIELDS, 0)
        observed_node_ids: set[str] = set()
        observed_node_keys: set[str] = set()
        batch_size = min(500, WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS + 1)
        for row in row_query.iterator(chunk_size=max(1, batch_size)):
            actual_node_count += 1
            if actual_node_count > WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit exceeds the retained-node limit"
                )
            if topology is None:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit found detail without a current topology"
                )
            _, _, has_fanout = _verify_stored_node_detail_row(
                row,
                identity=identity,
                maximum_topology_version=topology.topology_version,
                maximum_detail_revision=detail_revision,
            )
            node_id = str(row["node_id"])
            node_key = str(row["node_key"])
            if node_id in observed_node_ids or node_key in observed_node_keys:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit found duplicate node detail"
                )
            if node_id not in topology.node_ids:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit found detail outside the current topology"
                )
            if (node_id in topology.map_node_ids) != has_fanout:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit found detail fanout that conflicts "
                    "with the current topology"
                )
            observed_node_ids.add(node_id)
            observed_node_keys.add(node_key)
            actual_encoded_bytes += int(row["encoded_bytes"])
            actual_decoded_bytes += int(row["decoded_bytes"])
            actual_event_count += int(row["event_count"])
            actual_truncated_count += int(bool(row["truncated"]))
            actual_state_counts[str(row["state"])] += 1
            if actual_event_count > WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit exceeds the run-global event bound"
                )
            if (
                actual_encoded_bytes > WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES
                or actual_decoded_bytes > WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES
            ):
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit detail bytes exceed storage limits"
                )

        if actual_node_count != run_storage.detail_node_count:
            raise WorkflowProgressStorageIntegrityError(
                "workflow progress audit row count does not match the run aggregate"
            )
        if actual_encoded_bytes != run_storage.detail_encoded_bytes:
            raise WorkflowProgressStorageIntegrityError(
                "workflow progress audit encoded bytes do not match the run aggregate"
            )
        if actual_decoded_bytes != run_storage.detail_decoded_bytes:
            raise WorkflowProgressStorageIntegrityError(
                "workflow progress audit decoded bytes do not match the run aggregate"
            )
        if actual_state_counts != state_counts:
            raise WorkflowProgressStorageIntegrityError(
                "workflow progress audit state counts do not match run aggregates"
            )
        if actual_truncated_count != run_storage.detail_truncated_count:
            raise WorkflowProgressStorageIntegrityError(
                "workflow progress audit truncated count does not match the run aggregate"
            )
        if actual_event_count != run_storage.detail_event_count:
            raise WorkflowProgressStorageIntegrityError(
                "workflow progress audit event count does not match the run aggregate"
            )
        if topology is not None and actual_node_count < topology.node_count and not detail_reasons:
            raise WorkflowProgressStorageIntegrityError(
                "workflow progress audit is missing detail without truncation evidence"
            )
        if (
            topology is not None
            and actual_node_count == topology.node_count
            and observed_node_ids != topology.node_ids
        ):
            raise WorkflowProgressStorageIntegrityError(
                "workflow progress audit detail set does not match the current topology"
            )
        active_identity_matches = (
            execution.attempt_number == identity.attempt_number
            and execution.execution_generation == identity.execution_generation
            and execution.workflow_run_id is not None
            and str(execution.workflow_run_id) == identity.run_id
        )
        if active_identity_matches and detail_revision is not None:
            serialized_summary = execution.workflow_progress_summary_json
            if not isinstance(serialized_summary, str):
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit active run has no canonical summary"
                )
            try:
                active_summary = deserialize_workflow_progress_summary(
                    serialized_summary,
                    expected_identity=identity,
                )
                canonical_summary = serialize_workflow_progress_summary(
                    active_summary,
                    expected_identity=identity,
                )
            except WorkflowProgressSummaryError as error:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit active summary is invalid"
                ) from error
            if canonical_summary != serialized_summary:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit active summary is not canonical"
                )
            if topology is None:
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit active detail has no topology"
                )
            summary_node_counts = active_summary["node_counts"]
            summary_edge_counts = active_summary["edge_counts"]
            summary_reasons = set(active_summary["detail"]["truncation_reasons"])
            expected_summary_reasons = detail_reasons | set(topology.truncation_reasons)
            last_observed_terminal_detail = workflow_progress_detail_is_last_observed(
                active_summary
            )
            if last_observed_terminal_detail:
                expected_summary_reasons.add(
                    WorkflowProgressTruncationReason.TERMINAL_STATE_UNREPORTED.value
                )
            if actual_truncated_count:
                expected_summary_reasons.add(
                    WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value
                )
            summary_state_counts = {
                state: summary_node_counts[state.lower()]
                for state in _DETAIL_STATE_AGGREGATE_FIELDS
            }
            stored_expiry = run_storage.detail_expires_at
            if stored_expiry is not None and timezone.is_naive(stored_expiry):
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit run expiry is not timezone-aware"
                )
            stored_expiry_value = (
                stored_expiry.astimezone(UTC).isoformat().replace("+00:00", "Z")
                if stored_expiry is not None
                else None
            )
            if (
                active_summary["topology_version"] != topology.topology_version
                or active_summary["detail_revision"] != detail_revision
                or active_summary["storage"]["manifest_id"] != topology.manifest_id
                or summary_node_counts["retained_topology"] != topology.node_count
                or summary_node_counts["retained_detail"] != actual_node_count
                or summary_edge_counts["retained_topology"] != topology.edge_count
                or summary_reasons != expected_summary_reasons
                or active_summary["retention"]["detail_days"] != run_storage.detail_retention_days
                or active_summary["retention"]["detail_expires_at"] != stored_expiry_value
            ):
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit active summary conflicts with storage"
                )
            if not last_observed_terminal_detail and any(
                actual_state_counts[state] > summary_state_counts[state]
                for state in _DETAIL_STATE_AGGREGATE_FIELDS
            ):
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit retained state counts conflict with active summary"
                )
            if active_summary["detail"]["complete"] and any(
                summary_state_counts[state] != actual_state_counts[state]
                for state in _DETAIL_STATE_AGGREGATE_FIELDS
            ):
                raise WorkflowProgressStorageIntegrityError(
                    "workflow progress audit complete summary state counts conflict"
                )
        return WorkflowProgressDetailAuditResult(
            run_storage_id=run_storage.pk,
            topology_version=topology.topology_version if topology is not None else None,
            detail_revision=detail_revision,
            node_count=actual_node_count,
            encoded_bytes=actual_encoded_bytes,
            decoded_bytes=actual_decoded_bytes,
            event_count=actual_event_count,
            truncated_count=actual_truncated_count,
            state_counts=tuple(actual_state_counts.items()),
        )


def stamp_workflow_progress_detail_expiry_locked(
    execution: RayTaskExecution,
    serialized_summary: str | None,
    *,
    using: str | None = None,
) -> bool:
    """Stamp the exact terminal run with its authoritative or durable deadline.

    The caller must already hold the owning task-row lock.  A canonical terminal
    summary owns the exact deadline, including an extension beyond an earlier
    producer value.  If that summary is missing or corrupt, a terminal execution
    falls back to the policy persisted by its exact published run.
    """
    if execution.workflow_run_id is None:
        return False
    identity = WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=str(execution.workflow_run_id),
    )
    summary_valid = False
    detail_revision: int | None = None
    expiry: datetime | None = None
    if serialized_summary is not None:
        try:
            summary = deserialize_workflow_progress_summary(
                serialized_summary,
                expected_identity=identity,
            )
            summary_valid = (
                serialize_workflow_progress_summary(summary, expected_identity=identity)
                == serialized_summary
            )
        except WorkflowProgressSummaryError:
            summary_valid = False
        if summary_valid:
            expires_at = summary["retention"]["detail_expires_at"]
            if (
                summary["detail_revision"] is None
                or summary["state"] != execution.state
                or summary["terminal"]["outcome"] != execution.state
                or not isinstance(expires_at, str)
            ):
                return False
            detail_revision = int(summary["detail_revision"])
            expiry = datetime.fromisoformat(expires_at[:-1] + "+00:00")
    if not summary_valid and (
        execution.state not in WORKFLOW_PROGRESS_TERMINAL_STATES or execution.finished_at is None
    ):
        return False
    database = using or execution._state.db or "default"
    run_query = (
        WorkflowProgressRunStorage.objects.using(database)
        .select_for_update()
        .filter(
            execution_id=execution.pk,
            attempt_number=identity.attempt_number,
            execution_generation=identity.execution_generation,
            run_id=identity.run_id,
        )
    )
    if detail_revision is not None:
        run_query = run_query.filter(detail_revision=detail_revision)
    else:
        run_query = run_query.filter(detail_revision__isnull=False)
    run_storage = run_query.first()
    if run_storage is None:
        return False
    if expiry is None:
        finished_at = execution.finished_at
        if finished_at is None:
            return False
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=UTC)
        expiry = finished_at.astimezone(UTC) + timedelta(days=run_storage.detail_retention_days)
    if run_storage.detail_expires_at == expiry:
        return True
    run_storage.detail_expires_at = expiry
    run_storage.updated_at = timezone.now()
    run_storage.save(update_fields=["detail_expires_at", "updated_at"])
    return True


__all__ = [
    "PreparedWorkflowProgressDetail",
    "PreparedWorkflowProgressNodeDetail",
    "PreparedWorkflowProgressTopology",
    "PreparedWorkflowProgressTopologyPage",
    "VerifiedWorkflowProgressTopology",
    "VerifiedWorkflowProgressTopologyManifestRecord",
    "WorkflowProgressDetailAuditResult",
    "WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES",
    "WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES",
    "WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS",
    "WORKFLOW_PROGRESS_LIMITS_PROFILE",
    "WORKFLOW_PROGRESS_NODE_DETAIL_SCHEMA_VERSION",
    "WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION",
    "WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS",
    "WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES",
    "WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS",
    "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES",
    "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS",
    "WorkflowProgressStorageError",
    "WorkflowProgressStorageConflictError",
    "WorkflowProgressStorageIntegrityError",
    "WorkflowProgressStorageLimitError",
    "WorkflowProgressTopologyCollection",
    "WorkflowProgressPublicationResult",
    "audit_workflow_progress_detail_storage",
    "discard_workflow_progress_topology_candidate",
    "persist_workflow_progress_publication",
    "prepare_workflow_progress_detail",
    "prepare_workflow_progress_node_detail",
    "stage_workflow_progress_topology",
    "stamp_workflow_progress_detail_expiry_locked",
    "verify_workflow_progress_node_detail_record",
    "verify_workflow_progress_topology_manifest",
    "verify_workflow_progress_topology_manifest_record",
    "verify_workflow_progress_topology_page_record",
]
