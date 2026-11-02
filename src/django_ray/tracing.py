"""Optional, fail-open OpenTelemetry task and workflow correlation (#369).

This module provides opt-in distributed tracing across Django enqueue, worker
claim, Ray submission/execution, workflow coordination, retry reconciliation,
and durable terminal persistence without adding a required OpenTelemetry
dependency to the base ``django-ray`` installation.

Key invariants:
- Zero required third-party dependencies: works with an injected tracer protocol,
  optional ``opentelemetry-api`` when installed, or a zero-allocation no-op tracer.
- Fail-open isolation: tracer, context propagation, or exporter exceptions never
  fail, retry, block, or falsify authoritative PostgreSQL task state.
- Redaction and cardinality safety: task arguments, keyword arguments, results,
  serialized payloads, ``RuntimeEnv`` contents, credentials, raw error messages,
  and tracebacks are never attached to spans.
- Stale-attempt fencing: superseded attempts (stale ``execution_generation`` or
  ``attempt_number`` after lease loss or retry) cannot emit an authoritative
  terminal persistence span.
- Bounded workflow fan-out: per-run child node spans are capped at a configurable
  ceiling and summarized on the workflow execution span.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

W3C_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
MAX_TRACESTATE_BYTES = 512
MAX_TRACESTATE_MEMBERS = 32
MAX_BOUNDED_STRING_ATTR_LEN = 128
DEFAULT_MAX_WORKFLOW_NODE_SPANS = 32
MAX_WORKFLOW_NODE_SPANS_CEILING = 256

SPAN_TASK_ENQUEUE = "django_ray.task.enqueue"
SPAN_TASK_CLAIM = "django_ray.task.claim"
SPAN_TASK_RAY_SUBMIT = "django_ray.task.ray_submit"
SPAN_TASK_REMOTE_EXECUTE = "django_ray.task.remote_execute"
SPAN_TASK_RETRY_RECONCILE = "django_ray.task.retry_reconcile"
SPAN_WORKFLOW_EXECUTE = "django_ray.workflow.execute"
SPAN_WORKFLOW_NODE = "django_ray.workflow.node"
SPAN_TASK_TERMINAL_PERSIST = "django_ray.task.terminal_persist"

ALLOWED_SPAN_NAMES: frozenset[str] = frozenset(
    {
        SPAN_TASK_ENQUEUE,
        SPAN_TASK_CLAIM,
        SPAN_TASK_RAY_SUBMIT,
        SPAN_TASK_REMOTE_EXECUTE,
        SPAN_TASK_RETRY_RECONCILE,
        SPAN_WORKFLOW_EXECUTE,
        SPAN_WORKFLOW_NODE,
        SPAN_TASK_TERMINAL_PERSIST,
    }
)

ALLOWED_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
    {
        "django_ray.task.id",
        "django_ray.task.execution_pk",
        "django_ray.task.attempt_number",
        "django_ray.task.execution_generation",
        "django_ray.task.queue_name",
        "django_ray.task.state",
        "django_ray.task.runner_mode",
        "django_ray.task.cancellation_status",
        "django_ray.attempt.authoritative",
        "django_ray.attempt.superseded_reason",
        "django_ray.workflow.run_id",
        "django_ray.workflow.node_id",
        "django_ray.workflow.node_count",
        "django_ray.workflow.sampled_node_spans",
        "django_ray.workflow.omitted_node_spans",
        "django_ray.error.type",
        "django_ray.trace.context_status",
    }
)

FORBIDDEN_ATTRIBUTE_SUBSTRINGS: tuple[str, ...] = (
    "args",
    "kwargs",
    "result",
    "payload",
    "input_reference",
    "runtime_env",
    "traceback",
    "error_message",
    "secret",
    "token",
    "password",
    "authorization",
    "credential",
    "dsn",
    "cookie",
)


class SpanProtocol(Protocol):
    """Minimal protocol for an active or recorded correlation span."""

    def set_attribute(self, key: str, value: str | int | float | bool) -> None: ...

    def set_status(self, status_code: str, description: str = "") -> None: ...

    def end(self) -> None: ...


class TracerProtocol(Protocol):
    """Minimal protocol for starting correlation spans."""

    def start_span(
        self,
        name: str,
        *,
        parent_context: TraceContextEnvelope | None = None,
        attributes: dict[str, str | int | float | bool] | None = None,
    ) -> SpanProtocol: ...


@dataclass(frozen=True)
class TraceContextEnvelope:
    """Validated, bounded W3C trace context envelope for cross-boundary correlation."""

    traceparent: str
    trace_id: str
    span_id: str
    trace_flags: str
    tracestate: str = ""

    def to_carrier(self) -> dict[str, str]:
        carrier = {"traceparent": self.traceparent}
        if self.tracestate:
            carrier["tracestate"] = self.tracestate
        return carrier


@dataclass(frozen=True)
class TraceContextParseResult:
    """Outcome of parsing an untrusted trace context carrier."""

    envelope: TraceContextEnvelope | None
    status: str


@dataclass(frozen=True)
class AttemptAuthorityToken:
    """Fencing token identifying a specific task execution attempt and generation."""

    task_execution_pk: int
    attempt_number: int
    execution_generation: int

    def validate(self) -> None:
        if (
            type(self.task_execution_pk) is not int
            or self.task_execution_pk <= 0
            or type(self.attempt_number) is not int
            or self.attempt_number <= 0
            or type(self.execution_generation) is not int
            or self.execution_generation < 0
        ):
            raise ValueError(
                "AttemptAuthorityToken requires positive pk/attempt and >=0 generation"
            )


@dataclass(frozen=True)
class TracingConfig:
    """Configuration for optional OpenTelemetry task and workflow correlation."""

    enabled: bool = False
    sample_rate: float = 1.0
    max_workflow_node_spans_per_run: int = DEFAULT_MAX_WORKFLOW_NODE_SPANS

    def validated(self) -> TracingConfig:
        if type(self.enabled) is not bool:
            raise ValueError("TracingConfig.enabled must be a bool")
        if not isinstance(self.sample_rate, int | float) or isinstance(self.sample_rate, bool):
            raise ValueError("TracingConfig.sample_rate must be a float between 0.0 and 1.0")
        rate = float(self.sample_rate)
        if not 0.0 <= rate <= 1.0:
            raise ValueError("TracingConfig.sample_rate must be between 0.0 and 1.0")
        if (
            type(self.max_workflow_node_spans_per_run) is not int
            or not 0 <= self.max_workflow_node_spans_per_run <= MAX_WORKFLOW_NODE_SPANS_CEILING
        ):
            raise ValueError(
                f"TracingConfig.max_workflow_node_spans_per_run must be between 0 and "
                f"{MAX_WORKFLOW_NODE_SPANS_CEILING}"
            )
        return TracingConfig(
            enabled=self.enabled,
            sample_rate=rate,
            max_workflow_node_spans_per_run=self.max_workflow_node_spans_per_run,
        )


@dataclass
class TaskTracingDiagnostics:
    """Bounded operational counters for tracing correlation."""

    spans_started: int = 0
    spans_ended: int = 0
    exporter_error_count: int = 0
    redacted_attribute_count: int = 0
    fenced_stale_terminal_count: int = 0
    malformed_context_count: int = 0
    omitted_workflow_node_span_count: int = 0


def _validate_tracestate(raw_tracestate: Any) -> tuple[str | None, str]:
    if raw_tracestate is None or raw_tracestate == "":
        return "", "ok"
    if not isinstance(raw_tracestate, str):
        return None, "malformed_tracestate"
    encoded = raw_tracestate.encode("utf-8", errors="surrogatepass")
    if len(encoded) > MAX_TRACESTATE_BYTES:
        return None, "oversized_tracestate"
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in raw_tracestate):
        return None, "malformed_tracestate"
    lowered = raw_tracestate.lower()
    if any(forbidden in lowered for forbidden in FORBIDDEN_ATTRIBUTE_SUBSTRINGS):
        return None, "redacted_tracestate"
    members = [part.strip() for part in raw_tracestate.split(",") if part.strip()]
    if len(members) > MAX_TRACESTATE_MEMBERS:
        return None, "oversized_tracestate"
    for member in members:
        if "=" not in member:
            return None, "malformed_tracestate"
        key, val = member.split("=", 1)
        if not key or not val:
            return None, "malformed_tracestate"
    return ",".join(members), "ok"


def parse_trace_context(carrier: dict[str, Any] | None) -> TraceContextParseResult:
    """Validate and extract a W3C trace context from an untrusted carrier dict."""
    if not carrier:
        return TraceContextParseResult(envelope=None, status="missing_context")
    if not isinstance(carrier, dict):
        return TraceContextParseResult(envelope=None, status="malformed_traceparent")
    raw_tp = carrier.get("traceparent")
    if raw_tp is None:
        return TraceContextParseResult(envelope=None, status="missing_context")
    if not isinstance(raw_tp, str) or len(raw_tp) != 55:
        return TraceContextParseResult(envelope=None, status="malformed_traceparent")
    match = W3C_TRACEPARENT_RE.match(raw_tp.strip().lower())
    if match is None:
        return TraceContextParseResult(envelope=None, status="malformed_traceparent")
    version = match.group("version")
    trace_id = match.group("trace_id")
    span_id = match.group("span_id")
    flags = match.group("flags")
    if version == "ff" or trace_id == "0" * 32 or span_id == "0" * 16:
        return TraceContextParseResult(envelope=None, status="malformed_traceparent")

    tracestate, ts_status = _validate_tracestate(carrier.get("tracestate"))
    if tracestate is None:
        # Preserve valid traceparent even if tracestate was malformed/redacted.
        return TraceContextParseResult(
            envelope=TraceContextEnvelope(
                traceparent=f"{version}-{trace_id}-{span_id}-{flags}",
                trace_id=trace_id,
                span_id=span_id,
                trace_flags=flags,
                tracestate="",
            ),
            status=ts_status,
        )
    return TraceContextParseResult(
        envelope=TraceContextEnvelope(
            traceparent=f"{version}-{trace_id}-{span_id}-{flags}",
            trace_id=trace_id,
            span_id=span_id,
            trace_flags=flags,
            tracestate=tracestate,
        ),
        status="ok",
    )


def sanitize_span_attributes(
    raw_attributes: dict[str, Any] | None,
    diagnostics: TaskTracingDiagnostics | None = None,
) -> dict[str, str | int | float | bool]:
    """Filter and bound span attributes to the low-cardinality allowlist."""
    if not raw_attributes:
        return {}
    clean: dict[str, str | int | float | bool] = {}
    for key, value in raw_attributes.items():
        if not isinstance(key, str):
            if diagnostics is not None:
                diagnostics.redacted_attribute_count += 1
            continue
        lowered_key = key.lower()
        if key not in ALLOWED_ATTRIBUTE_KEYS or any(
            sub in lowered_key for sub in FORBIDDEN_ATTRIBUTE_SUBSTRINGS
        ):
            if diagnostics is not None:
                diagnostics.redacted_attribute_count += 1
            continue
        if isinstance(value, bool):
            clean[key] = value
        elif type(value) is int:
            clean[key] = value
        elif type(value) is float:
            clean[key] = value
        elif isinstance(value, str):
            bounded = value[:MAX_BOUNDED_STRING_ATTR_LEN]
            clean[key] = bounded
        else:
            if diagnostics is not None:
                diagnostics.redacted_attribute_count += 1
    return clean


@dataclass
class RecordedSpan:
    """In-memory recorded span for testing and local inspection."""

    name: str
    parent_context: TraceContextEnvelope | None = None
    attributes: dict[str, str | int | float | bool] = field(default_factory=dict)
    status_code: str = "UNSET"
    status_description: str = ""
    ended: bool = False

    def set_attribute(self, key: str, value: str | int | float | bool) -> None:
        self.attributes[key] = value

    def set_status(self, status_code: str, description: str = "") -> None:
        self.status_code = status_code
        self.status_description = description[:MAX_BOUNDED_STRING_ATTR_LEN]

    def end(self) -> None:
        self.ended = True


class InMemoryTracer:
    """Deterministic in-memory tracer implementation for verification."""

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    def start_span(
        self,
        name: str,
        *,
        parent_context: TraceContextEnvelope | None = None,
        attributes: dict[str, str | int | float | bool] | None = None,
    ) -> RecordedSpan:
        span = RecordedSpan(
            name=name,
            parent_context=parent_context,
            attributes=dict(attributes or {}),
        )
        self.spans.append(span)
        return span


class _NoOpSpan:
    __slots__ = ()

    def set_attribute(self, key: str, value: str | int | float | bool) -> None:
        return None

    def set_status(self, status_code: str, description: str = "") -> None:
        return None

    def end(self) -> None:
        return None


_NOOP_SPAN = _NoOpSpan()


class TaskTracingCoordinator:
    """Fail-open coordinator for task and workflow lifecycle spans."""

    def __init__(
        self,
        config: TracingConfig | None = None,
        *,
        tracer: TracerProtocol | None = None,
    ) -> None:
        self.config = (config or TracingConfig()).validated()
        self._tracer = tracer
        self.diagnostics = TaskTracingDiagnostics()

    def _is_sampled(self, deterministic_key: int = 0) -> bool:
        if not self.config.enabled or self._tracer is None:
            return False
        if self.config.sample_rate <= 0.0:
            return False
        if self.config.sample_rate >= 1.0:
            return True
        bucket = ((deterministic_key * 2654435761) & 0xFFFFFFFF) / 0xFFFFFFFF
        return bucket < self.config.sample_rate

    def extract_context(self, carrier: dict[str, Any] | None) -> TraceContextEnvelope | None:
        result = parse_trace_context(carrier)
        if result.status not in ("ok", "missing_context"):
            self.diagnostics.malformed_context_count += 1
        return result.envelope

    def _safe_start_span(
        self,
        name: str,
        *,
        parent_context: TraceContextEnvelope | None = None,
        raw_attributes: dict[str, Any] | None = None,
        sample_key: int = 0,
    ) -> SpanProtocol:
        if name not in ALLOWED_SPAN_NAMES:
            raise ValueError(f"Unsupported django-ray span name: {name!r}")
        attrs = sanitize_span_attributes(raw_attributes, self.diagnostics)
        if not self._is_sampled(sample_key):
            return _NOOP_SPAN
        assert self._tracer is not None
        try:
            span = self._tracer.start_span(
                name,
                parent_context=parent_context,
                attributes=attrs,
            )
            self.diagnostics.spans_started += 1
            return span
        except Exception:
            self.diagnostics.exporter_error_count += 1
            return _NOOP_SPAN

    def safe_end_span(
        self,
        span: SpanProtocol,
        *,
        status_code: str = "OK",
        error: BaseException | None = None,
        extra_attributes: dict[str, Any] | None = None,
    ) -> None:
        if span is _NOOP_SPAN:
            return
        try:
            if extra_attributes:
                clean = sanitize_span_attributes(extra_attributes, self.diagnostics)
                for k, v in clean.items():
                    span.set_attribute(k, v)
            if error is not None:
                span.set_attribute("django_ray.error.type", type(error).__name__)
                span.set_status("ERROR", type(error).__name__)
            else:
                span.set_status(status_code)
            span.end()
            self.diagnostics.spans_ended += 1
        except Exception:
            self.diagnostics.exporter_error_count += 1

    def trace_lifecycle_stage(
        self,
        stage_span_name: str,
        *,
        authority: AttemptAuthorityToken,
        carrier: dict[str, Any] | None = None,
        task_id: str | None = None,
        queue_name: str | None = None,
        state: str | None = None,
        runner_mode: str | None = None,
        extra_attributes: dict[str, Any] | None = None,
    ) -> SpanProtocol:
        authority.validate()
        parse_res = parse_trace_context(carrier)
        if parse_res.status not in ("ok", "missing_context"):
            self.diagnostics.malformed_context_count += 1
        attrs: dict[str, Any] = {
            "django_ray.task.execution_pk": authority.task_execution_pk,
            "django_ray.task.attempt_number": authority.attempt_number,
            "django_ray.task.execution_generation": authority.execution_generation,
            "django_ray.trace.context_status": parse_res.status,
        }
        if task_id is not None:
            attrs["django_ray.task.id"] = task_id
        if queue_name is not None:
            attrs["django_ray.task.queue_name"] = queue_name
        if state is not None:
            attrs["django_ray.task.state"] = state
        if runner_mode is not None:
            attrs["django_ray.task.runner_mode"] = runner_mode
        if extra_attributes:
            attrs.update(extra_attributes)
        return self._safe_start_span(
            stage_span_name,
            parent_context=parse_res.envelope,
            raw_attributes=attrs,
            sample_key=authority.task_execution_pk,
        )

    def record_terminal_persist(
        self,
        *,
        attempt_authority: AttemptAuthorityToken,
        current_db_authority: AttemptAuthorityToken,
        lease_owned: bool,
        terminal_state: str,
        cancellation_status: str | None = None,
        carrier: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> tuple[bool, SpanProtocol]:
        """Record terminal persistence only if the attempt still holds DB authority.

        If the attempt was superseded by a newer ``execution_generation`` or
        ``attempt_number`` or lost its worker lease, this method fences the
        stale attempt and refuses to emit an authoritative terminal span.
        """
        attempt_authority.validate()
        current_db_authority.validate()
        if attempt_authority.task_execution_pk != current_db_authority.task_execution_pk:
            raise ValueError("Authority tokens must reference the same task_execution_pk")

        superseded_reason: str | None = None
        if not lease_owned:
            superseded_reason = "lease_lost"
        elif (
            attempt_authority.execution_generation != current_db_authority.execution_generation
            or attempt_authority.attempt_number != current_db_authority.attempt_number
        ):
            superseded_reason = "stale_generation"

        if superseded_reason is not None:
            self.diagnostics.fenced_stale_terminal_count += 1
            fenced_span = self.trace_lifecycle_stage(
                SPAN_TASK_RETRY_RECONCILE,
                authority=attempt_authority,
                carrier=carrier,
                state=terminal_state,
                extra_attributes={
                    "django_ray.attempt.authoritative": False,
                    "django_ray.attempt.superseded_reason": superseded_reason,
                },
            )
            self.safe_end_span(fenced_span, status_code="OK")
            return False, fenced_span

        extra: dict[str, Any] = {
            "django_ray.attempt.authoritative": True,
        }
        if cancellation_status is not None:
            extra["django_ray.task.cancellation_status"] = cancellation_status

        span = self.trace_lifecycle_stage(
            SPAN_TASK_TERMINAL_PERSIST,
            authority=attempt_authority,
            carrier=carrier,
            state=terminal_state,
            extra_attributes=extra,
        )
        self.safe_end_span(span, status_code="OK", error=error)
        return True, span

    def trace_workflow_execution(
        self,
        *,
        authority: AttemptAuthorityToken,
        workflow_run_id: str,
        node_ids: list[str] | tuple[str, ...],
        carrier: dict[str, Any] | None = None,
    ) -> tuple[SpanProtocol, list[SpanProtocol]]:
        """Emit a parent workflow span and a bounded number of child node spans."""
        authority.validate()
        total_nodes = len(node_ids)
        budget = self.config.max_workflow_node_spans_per_run
        sampled_ids = list(node_ids[:budget])
        omitted_count = max(0, total_nodes - len(sampled_ids))
        if omitted_count > 0:
            self.diagnostics.omitted_workflow_node_span_count += omitted_count

        parent_span = self.trace_lifecycle_stage(
            SPAN_WORKFLOW_EXECUTE,
            authority=authority,
            carrier=carrier,
            extra_attributes={
                "django_ray.workflow.run_id": workflow_run_id,
                "django_ray.workflow.node_count": total_nodes,
                "django_ray.workflow.sampled_node_spans": len(sampled_ids),
                "django_ray.workflow.omitted_node_spans": omitted_count,
            },
        )
        child_spans: list[SpanProtocol] = []
        parse_res = parse_trace_context(carrier)
        for node_id in sampled_ids:
            child_span = self._safe_start_span(
                SPAN_WORKFLOW_NODE,
                parent_context=parse_res.envelope,
                raw_attributes={
                    "django_ray.task.execution_pk": authority.task_execution_pk,
                    "django_ray.task.attempt_number": authority.attempt_number,
                    "django_ray.task.execution_generation": authority.execution_generation,
                    "django_ray.workflow.run_id": workflow_run_id,
                    "django_ray.workflow.node_id": str(node_id),
                },
                sample_key=authority.task_execution_pk,
            )
            self.safe_end_span(child_span, status_code="OK")
            child_spans.append(child_span)

        self.safe_end_span(parent_span, status_code="OK")
        return parent_span, child_spans


__all__ = [
    "ALLOWED_ATTRIBUTE_KEYS",
    "ALLOWED_SPAN_NAMES",
    "DEFAULT_MAX_WORKFLOW_NODE_SPANS",
    "MAX_WORKFLOW_NODE_SPANS_CEILING",
    "SPAN_TASK_CLAIM",
    "SPAN_TASK_ENQUEUE",
    "SPAN_TASK_RAY_SUBMIT",
    "SPAN_TASK_REMOTE_EXECUTE",
    "SPAN_TASK_RETRY_RECONCILE",
    "SPAN_TASK_TERMINAL_PERSIST",
    "SPAN_WORKFLOW_EXECUTE",
    "SPAN_WORKFLOW_NODE",
    "AttemptAuthorityToken",
    "InMemoryTracer",
    "RecordedSpan",
    "SpanProtocol",
    "TaskTracingCoordinator",
    "TaskTracingDiagnostics",
    "TraceContextEnvelope",
    "TraceContextParseResult",
    "TracerProtocol",
    "TracingConfig",
    "parse_trace_context",
    "sanitize_span_attributes",
]
