"""Task and workflow tracing primitives and optional OpenTelemetry adapter (#369, #826).

Callers may supply any ``TracerProtocol`` adapter (including the built-in
lazy-importing ``OpenTelemetryTracer``) or invoke ``emit_task_lifecycle_span``
at lifecycle boundaries. This module imports only the standard library at module
import time; ``OpenTelemetryTracer`` lazily imports ``opentelemetry.trace`` and
degrades to a strict no-op when OpenTelemetry is unavailable. Install the
optional ``django-ray[otel]`` extra for the OpenTelemetry SDK.

Helper contracts:
- Tracer failure isolation: exceptions during span start, update, and end are
  caught. Callers retain responsibility for runtime integration and authority.
- Redaction and cardinality bounds: the attribute allowlist excludes task payloads,
  ``RuntimeEnv`` contents, credentials, raw error messages, and tracebacks.
- Stale-attempt fencing: terminal span emission compares caller-supplied authority
  tokens and lease ownership; the helper does not fetch database state.
- Bounded workflow span creation: each helper call caps child spans, links them as
  children of the workflow span, and ends them immediately.
"""

from __future__ import annotations

import re
import sys
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol

W3C_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
MAX_TRACESTATE_BYTES = 512
MAX_TRACESTATE_MEMBERS = 32
MAX_BOUNDED_STRING_ATTR_LEN = 128
DEFAULT_MAX_WORKFLOW_NODE_SPANS = 100
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

LifecycleTraceEvent = Literal["enqueued", "started", "finished"]

LIFECYCLE_EVENT_SPAN_NAMES: Mapping[LifecycleTraceEvent, str] = MappingProxyType(
    {
        "enqueued": SPAN_TASK_ENQUEUE,
        "started": SPAN_TASK_REMOTE_EXECUTE,
        "finished": SPAN_TASK_TERMINAL_PERSIST,
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
    if not isinstance(raw_tp, str):
        return TraceContextParseResult(envelope=None, status="malformed_traceparent")
    normalized_tp = raw_tp.strip().lower()
    if len(normalized_tp) != 55:
        return TraceContextParseResult(envelope=None, status="malformed_traceparent")
    match = W3C_TRACEPARENT_RE.match(normalized_tp)
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
    trace_id: str = ""
    span_id: str = ""
    trace_flags: str = "01"
    tracestate: str = ""

    def set_attribute(self, key: str, value: str | int | float | bool) -> None:
        self.attributes[key] = value

    def set_status(self, status_code: str, description: str = "") -> None:
        self.status_code = status_code
        self.status_description = description[:MAX_BOUNDED_STRING_ATTR_LEN]

    def end(self) -> None:
        self.ended = True

    def trace_context(self) -> TraceContextEnvelope | None:
        if len(self.trace_id) == 32 and len(self.span_id) == 16:
            return TraceContextEnvelope(
                traceparent=f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}",
                trace_id=self.trace_id,
                span_id=self.span_id,
                trace_flags=self.trace_flags,
                tracestate=self.tracestate,
            )
        return None


class InMemoryTracer:
    """Deterministic in-memory tracer implementation for verification."""

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []
        self._trace_seq = 0
        self._span_seq = 0

    def start_span(
        self,
        name: str,
        *,
        parent_context: TraceContextEnvelope | None = None,
        attributes: dict[str, str | int | float | bool] | None = None,
    ) -> RecordedSpan:
        self._span_seq += 1
        span_id = f"{self._span_seq:016x}"
        if parent_context is not None:
            trace_id = parent_context.trace_id
            trace_flags = parent_context.trace_flags
            tracestate = parent_context.tracestate
        else:
            self._trace_seq += 1
            trace_id = f"{self._trace_seq:032x}"
            trace_flags = "01"
            tracestate = ""
        span = RecordedSpan(
            name=name,
            parent_context=parent_context,
            attributes=dict(attributes or {}),
            trace_id=trace_id,
            span_id=span_id,
            trace_flags=trace_flags,
            tracestate=tracestate,
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


def _load_otel_trace_module() -> Any | None:
    """Lazily import ``opentelemetry.trace`` without failing when absent."""
    try:
        from opentelemetry import trace as otel_trace
    except Exception:
        return None
    return otel_trace


def _parse_otel_tracestate(otel_trace: Any, raw_tracestate: str) -> Any:
    trace_state_cls = getattr(otel_trace, "TraceState", None)
    if trace_state_cls is None:
        return None
    if not raw_tracestate:
        return trace_state_cls()
    entries: list[tuple[str, str]] = []
    for part in raw_tracestate.split(","):
        item = part.strip()
        if "=" in item:
            key, val = item.split("=", 1)
            if key and val:
                entries.append((key, val))
    try:
        return trace_state_cls(entries)
    except Exception:
        return trace_state_cls()


class _OpenTelemetrySpan:
    """Adapter wrapping an ``opentelemetry.trace.Span`` for ``SpanProtocol``."""

    def __init__(self, otel_span: Any, otel_trace: Any, *, tracestate: str = "") -> None:
        self._span = otel_span
        self._otel_trace = otel_trace
        self._tracestate = tracestate

    def set_attribute(self, key: str, value: str | int | float | bool) -> None:
        self._span.set_attribute(key, value)

    def set_status(self, status_code: str, description: str = "") -> None:
        status_code_enum = getattr(self._otel_trace, "StatusCode", None)
        status_cls = getattr(self._otel_trace, "Status", None)
        if status_code_enum is None:
            return
        if status_code == "ERROR":
            code = status_code_enum.ERROR
            bounded = description[:MAX_BOUNDED_STRING_ATTR_LEN]
            if status_cls is not None:
                self._span.set_status(status_cls(code, bounded))
            else:
                self._span.set_status(code, bounded)
        elif status_code == "OK":
            code = status_code_enum.OK
            if status_cls is not None:
                self._span.set_status(status_cls(code))
            else:
                self._span.set_status(code)
        else:
            code = status_code_enum.UNSET
            if status_cls is not None:
                self._span.set_status(status_cls(code))
            else:
                self._span.set_status(code)

    def end(self) -> None:
        self._span.end()

    def trace_context(self) -> TraceContextEnvelope | None:
        try:
            ctx = self._span.get_span_context()
        except Exception:
            return None
        if ctx is None:
            return None
        trace_id_int = getattr(ctx, "trace_id", 0)
        span_id_int = getattr(ctx, "span_id", 0)
        if not trace_id_int or not span_id_int:
            return None
        trace_id = f"{trace_id_int:032x}"
        span_id = f"{span_id_int:016x}"
        raw_flags = int(getattr(ctx, "trace_flags", 0)) & 0xFF
        flags = f"{raw_flags:02x}"
        tracestate = self._tracestate
        if not tracestate:
            ctx_ts = getattr(ctx, "trace_state", None)
            if ctx_ts is not None and hasattr(ctx_ts, "to_header"):
                try:
                    tracestate = str(ctx_ts.to_header() or "")
                except Exception:
                    tracestate = ""
        return TraceContextEnvelope(
            traceparent=f"00-{trace_id}-{span_id}-{flags}",
            trace_id=trace_id,
            span_id=span_id,
            trace_flags=flags,
            tracestate=tracestate,
        )


class OpenTelemetryTracer:
    """Lazy-importing OpenTelemetry ``TracerProtocol`` adapter.

    Degrades to a strict no-op when ``opentelemetry`` is not installed. Install
    ``django-ray[otel]`` to configure an OpenTelemetry SDK ``TracerProvider``.
    """

    def __init__(
        self,
        *,
        tracer_provider: Any | None = None,
        instrumentation_name: str = "django_ray",
        instrumentation_version: str | None = None,
    ) -> None:
        self._tracer_provider = tracer_provider
        self._instrumentation_name = instrumentation_name
        self._instrumentation_version = instrumentation_version

    @property
    def available(self) -> bool:
        """Return whether ``opentelemetry.trace`` can be imported."""
        return _load_otel_trace_module() is not None

    def _resolve_tracer(self, otel_trace: Any) -> Any:
        provider = self._tracer_provider if self._tracer_provider is not None else otel_trace
        if self._instrumentation_version is None:
            return provider.get_tracer(self._instrumentation_name)
        return provider.get_tracer(
            self._instrumentation_name,
            self._instrumentation_version,
        )

    def start_span(
        self,
        name: str,
        *,
        parent_context: TraceContextEnvelope | None = None,
        attributes: dict[str, str | int | float | bool] | None = None,
    ) -> SpanProtocol:
        otel_trace = _load_otel_trace_module()
        if otel_trace is None:
            return _NOOP_SPAN
        tracer = self._resolve_tracer(otel_trace)
        otel_ctx: Any = None
        tracestate_str = ""
        if parent_context is not None:
            tracestate_str = parent_context.tracestate
            trace_state = _parse_otel_tracestate(otel_trace, tracestate_str)
            span_ctx = otel_trace.SpanContext(
                trace_id=int(parent_context.trace_id, 16),
                span_id=int(parent_context.span_id, 16),
                is_remote=True,
                trace_flags=otel_trace.TraceFlags(int(parent_context.trace_flags, 16)),
                trace_state=trace_state,
            )
            otel_ctx = otel_trace.set_span_in_context(otel_trace.NonRecordingSpan(span_ctx))
        otel_span = tracer.start_span(
            name,
            context=otel_ctx,
            attributes=dict(attributes) if attributes else None,
        )
        return _OpenTelemetrySpan(otel_span, otel_trace, tracestate=tracestate_str)


def _span_trace_context(span: SpanProtocol) -> TraceContextEnvelope | None:
    getter = getattr(span, "trace_context", None)
    if callable(getter):
        try:
            ctx = getter()
            if isinstance(ctx, TraceContextEnvelope):
                return ctx
        except Exception:
            return None
    return None


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
        child_parent_context = _span_trace_context(parent_span)
        if child_parent_context is None and carrier is not None:
            child_parent_context = parse_trace_context(carrier).envelope
        for node_id in sampled_ids:
            child_span = self._safe_start_span(
                SPAN_WORKFLOW_NODE,
                parent_context=child_parent_context,
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


_TASK_TRACING_COORDINATOR: TaskTracingCoordinator | None = None
_CACHED_COORDINATOR_LOCK = threading.Lock()
_CACHED_SETTINGS_COORDINATOR_KEY: tuple[float, int] | None = None
_CACHED_SETTINGS_COORDINATOR: TaskTracingCoordinator | None = None
_LIFECYCLE_TRACE_CARRIER_LOCK = threading.Lock()
_LIFECYCLE_TRACE_CARRIERS: OrderedDict[int, dict[str, str]] = OrderedDict()
_LIFECYCLE_TRACE_CARRIER_MAX_ENTRIES = 1024
_ERROR_TERMINAL_STATES: frozenset[str] = frozenset({"FAILED", "LOST", "EXPIRED"})


def clear_tracing_caches() -> None:
    """Clear cached settings-resolved coordinator and lifecycle carrier links."""
    global _CACHED_SETTINGS_COORDINATOR, _CACHED_SETTINGS_COORDINATOR_KEY
    with _CACHED_COORDINATOR_LOCK:
        _CACHED_SETTINGS_COORDINATOR = None
        _CACHED_SETTINGS_COORDINATOR_KEY = None
    with _LIFECYCLE_TRACE_CARRIER_LOCK:
        _LIFECYCLE_TRACE_CARRIERS.clear()


def configure_task_tracing(coordinator: TaskTracingCoordinator | None) -> None:
    """Register or clear the process-wide coordinator used by ``emit_task_lifecycle_span``."""
    global _TASK_TRACING_COORDINATOR, _CACHED_SETTINGS_COORDINATOR, _CACHED_SETTINGS_COORDINATOR_KEY
    _TASK_TRACING_COORDINATOR = coordinator
    with _CACHED_COORDINATOR_LOCK:
        _CACHED_SETTINGS_COORDINATOR = None
        _CACHED_SETTINGS_COORDINATOR_KEY = None


def get_task_tracing_coordinator() -> TaskTracingCoordinator | None:
    """Return the explicitly registered task tracing coordinator, if any."""
    return _TASK_TRACING_COORDINATOR


def _resolve_lifecycle_coordinator() -> TaskTracingCoordinator | None:
    global _CACHED_SETTINGS_COORDINATOR, _CACHED_SETTINGS_COORDINATOR_KEY
    if _TASK_TRACING_COORDINATOR is not None:
        return _TASK_TRACING_COORDINATOR
    django_conf = sys.modules.get("django.conf")
    if django_conf is None:
        return None
    settings_obj = getattr(django_conf, "settings", None)
    if settings_obj is None or not getattr(settings_obj, "configured", False):
        return None
    ray_settings = getattr(settings_obj, "DJANGO_RAY", None)
    if not isinstance(ray_settings, Mapping) or ray_settings.get("TRACING_ENABLED") is not True:
        return None
    sample_rate = ray_settings.get("TRACING_SAMPLE_RATE", 1.0)
    max_nodes = ray_settings.get("TRACING_MAX_WORKFLOW_NODE_SPANS", DEFAULT_MAX_WORKFLOW_NODE_SPANS)
    config = TracingConfig(
        enabled=True,
        sample_rate=sample_rate,
        max_workflow_node_spans_per_run=max_nodes,
    ).validated()
    cache_key = (config.sample_rate, config.max_workflow_node_spans_per_run)
    with _CACHED_COORDINATOR_LOCK:
        if (
            _CACHED_SETTINGS_COORDINATOR is not None
            and _CACHED_SETTINGS_COORDINATOR_KEY == cache_key
        ):
            return _CACHED_SETTINGS_COORDINATOR
        coordinator = TaskTracingCoordinator(config, tracer=OpenTelemetryTracer())
        _CACHED_SETTINGS_COORDINATOR = coordinator
        _CACHED_SETTINGS_COORDINATOR_KEY = cache_key
        return coordinator


def _extract_active_otel_carrier() -> dict[str, str] | None:
    """Extract a W3C traceparent carrier from the active OTel span if loaded."""
    otel_trace = sys.modules.get("opentelemetry.trace")
    if otel_trace is None:
        return None
    get_current_span = getattr(otel_trace, "get_current_span", None)
    if not callable(get_current_span):
        return None
    try:
        current = get_current_span()
        ctx = current.get_span_context() if current is not None else None
        if ctx is None or not getattr(ctx, "is_valid", False):
            return None
        trace_id = int(ctx.trace_id)
        span_id = int(ctx.span_id)
        if trace_id <= 0 or span_id <= 0:
            return None
        flags = int(getattr(ctx, "trace_flags", 1)) & 0xFF
        return {"traceparent": f"00-{trace_id:032x}-{span_id:016x}-{flags:02x}"}
    except Exception:
        return None


def _remember_lifecycle_carrier(execution_pk: int, carrier: dict[str, str]) -> None:
    with _LIFECYCLE_TRACE_CARRIER_LOCK:
        _LIFECYCLE_TRACE_CARRIERS[execution_pk] = dict(carrier)
        _LIFECYCLE_TRACE_CARRIERS.move_to_end(execution_pk)
        while len(_LIFECYCLE_TRACE_CARRIERS) > _LIFECYCLE_TRACE_CARRIER_MAX_ENTRIES:
            _LIFECYCLE_TRACE_CARRIERS.popitem(last=False)


def _lookup_lifecycle_carrier(execution_pk: int) -> dict[str, str] | None:
    with _LIFECYCLE_TRACE_CARRIER_LOCK:
        stored = _LIFECYCLE_TRACE_CARRIERS.get(execution_pk)
        if stored is not None:
            _LIFECYCLE_TRACE_CARRIERS.move_to_end(execution_pk)
            return dict(stored)
        return None


def _forget_lifecycle_carrier(execution_pk: int) -> None:
    with _LIFECYCLE_TRACE_CARRIER_LOCK:
        _LIFECYCLE_TRACE_CARRIERS.pop(execution_pk, None)


def emit_task_lifecycle_span(
    event: LifecycleTraceEvent,
    execution: Any,
) -> None:
    """Emit one bounded, exception-isolated task lifecycle span if tracing is enabled.

    Designed for opt-in runtime wiring at task enqueue, start, and terminal
    boundaries. Always degrades to a strict no-op when tracing is disabled, when
    ``opentelemetry`` is uninstalled, or if extraction or export raises.
    """
    try:
        span_name = LIFECYCLE_EVENT_SPAN_NAMES.get(event)
        if span_name is None or execution is None:
            return
        coordinator = _resolve_lifecycle_coordinator()
        if coordinator is None or not coordinator.config.enabled:
            return

        raw_pk = getattr(execution, "pk", None)
        if raw_pk is None:
            raw_pk = getattr(execution, "id", None)
        if type(raw_pk) is not int or raw_pk <= 0:
            return

        raw_attempt = getattr(execution, "attempt_number", 1)
        attempt_number = raw_attempt if type(raw_attempt) is int and raw_attempt > 0 else 1
        raw_generation = getattr(execution, "execution_generation", 0)
        execution_generation = (
            raw_generation if type(raw_generation) is int and raw_generation >= 0 else 0
        )
        authority = AttemptAuthorityToken(
            task_execution_pk=raw_pk,
            attempt_number=attempt_number,
            execution_generation=execution_generation,
        )

        raw_task_id = getattr(execution, "task_id", None)
        task_id = str(raw_task_id) if raw_task_id is not None else None
        raw_queue = getattr(execution, "queue_name", None)
        queue_name = raw_queue if isinstance(raw_queue, str) else None
        raw_state = getattr(execution, "state", None)
        state = str(raw_state) if raw_state is not None else None
        raw_runner_mode = getattr(execution, "runner_mode", None)
        runner_mode = str(raw_runner_mode) if raw_runner_mode is not None else None

        raw_carrier = getattr(execution, "trace_context", None)
        carrier: dict[str, str] | None = (
            dict(raw_carrier) if isinstance(raw_carrier, Mapping) else None
        )
        if carrier is None:
            if event in ("started", "finished"):
                carrier = _lookup_lifecycle_carrier(raw_pk)
            if carrier is None:
                carrier = _extract_active_otel_carrier()

        extra_attrs: dict[str, Any] = {}
        if event == "finished":
            extra_attrs["django_ray.attempt.authoritative"] = True
            raw_cancel = getattr(execution, "cancellation_status", None)
            if isinstance(raw_cancel, str) and raw_cancel:
                extra_attrs["django_ray.task.cancellation_status"] = raw_cancel

        span = coordinator.trace_lifecycle_stage(
            span_name,
            authority=authority,
            carrier=carrier,
            task_id=task_id,
            queue_name=queue_name,
            state=state,
            runner_mode=runner_mode,
            extra_attributes=extra_attrs or None,
        )
        if event == "enqueued":
            emitted_ctx = _span_trace_context(span)
            if emitted_ctx is not None:
                _remember_lifecycle_carrier(raw_pk, emitted_ctx.to_carrier())
        elif event == "started":
            if _lookup_lifecycle_carrier(raw_pk) is None:
                emitted_ctx = _span_trace_context(span)
                if emitted_ctx is not None:
                    _remember_lifecycle_carrier(raw_pk, emitted_ctx.to_carrier())
        elif event == "finished":
            _forget_lifecycle_carrier(raw_pk)
        if event == "finished" and state in _ERROR_TERMINAL_STATES:
            error_type = getattr(execution, "error_type", None)
            error_label = (
                error_type[:MAX_BOUNDED_STRING_ATTR_LEN]
                if isinstance(error_type, str) and error_type
                else state
            )
            if span is not _NOOP_SPAN:
                try:
                    span.set_attribute("django_ray.error.type", error_label)
                    span.set_status("ERROR", error_label)
                    span.end()
                    coordinator.diagnostics.spans_ended += 1
                except Exception:
                    coordinator.diagnostics.exporter_error_count += 1
            return
        coordinator.safe_end_span(span, status_code="OK")
    except Exception:
        return


__all__ = [
    "ALLOWED_ATTRIBUTE_KEYS",
    "ALLOWED_SPAN_NAMES",
    "DEFAULT_MAX_WORKFLOW_NODE_SPANS",
    "LIFECYCLE_EVENT_SPAN_NAMES",
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
    "LifecycleTraceEvent",
    "OpenTelemetryTracer",
    "RecordedSpan",
    "SpanProtocol",
    "TaskTracingCoordinator",
    "TaskTracingDiagnostics",
    "TraceContextEnvelope",
    "TraceContextParseResult",
    "TracerProtocol",
    "TracingConfig",
    "clear_tracing_caches",
    "configure_task_tracing",
    "emit_task_lifecycle_span",
    "get_task_tracing_coordinator",
    "parse_trace_context",
    "sanitize_span_attributes",
]
