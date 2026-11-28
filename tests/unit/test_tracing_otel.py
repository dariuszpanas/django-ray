"""Unit tests for the OpenTelemetry tracer adapter, workflow parent-child spans, and lifecycle hook (#826)."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from django_ray.tracing import (
    SPAN_TASK_ENQUEUE,
    SPAN_TASK_REMOTE_EXECUTE,
    SPAN_TASK_TERMINAL_PERSIST,
    SPAN_WORKFLOW_EXECUTE,
    SPAN_WORKFLOW_NODE,
    AttemptAuthorityToken,
    InMemoryTracer,
    OpenTelemetryTracer,
    RecordedSpan,
    TaskTracingCoordinator,
    TracingConfig,
    configure_task_tracing,
    emit_task_lifecycle_span,
    get_task_tracing_coordinator,
    parse_trace_context,
)

CARRIER_TRACE_ID = "11112222333344445555666677778888"
CARRIER_SPAN_ID = "1111222233334444"
VALID_TRACEPARENT = f"00-{CARRIER_TRACE_ID}-{CARRIER_SPAN_ID}-01"


def _make_sdk_tracer() -> tuple[TracerProvider, InMemorySpanExporter, OpenTelemetryTracer]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = OpenTelemetryTracer(tracer_provider=provider, instrumentation_version="0.8.0")
    return provider, exporter, tracer


def test_tracing_module_has_no_eager_otel_or_django_imports() -> None:
    """django_ray.tracing must remain pure stdlib at module import time."""
    tracing_path = Path(__file__).parents[2] / "src" / "django_ray" / "tracing.py"
    tree = ast.parse(tracing_path.read_text(encoding="utf-8"))
    top_level_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_imports.add(node.module.split(".")[0])

    assert "opentelemetry" not in top_level_imports
    assert "django" not in top_level_imports
    assert "ray" not in top_level_imports


def test_parse_trace_context_strips_whitespace_before_length_check() -> None:
    padded = f"  \t00-{CARRIER_TRACE_ID.upper()}-{CARRIER_SPAN_ID.upper()}-01\r\n"
    result = parse_trace_context({"traceparent": padded, "tracestate": "vendor=ok"})
    assert result.status == "ok"
    assert result.envelope is not None
    assert result.envelope.traceparent == VALID_TRACEPARENT
    assert result.envelope.trace_id == CARRIER_TRACE_ID
    assert result.envelope.span_id == CARRIER_SPAN_ID
    assert result.envelope.tracestate == "vendor=ok"

    assert parse_trace_context({"traceparent": "   "}).status == "malformed_traceparent"
    assert (
        parse_trace_context({"traceparent": f" {VALID_TRACEPARENT}0 "}).status
        == "malformed_traceparent"
    )


def test_in_memory_workflow_execution_links_child_node_spans_to_workflow_parent() -> None:
    tracer = InMemoryTracer()
    coord = TaskTracingCoordinator(TracingConfig(enabled=True), tracer=tracer)
    authority = AttemptAuthorityToken(
        task_execution_pk=11, attempt_number=1, execution_generation=0
    )

    parent, children = coord.trace_workflow_execution(
        authority=authority,
        workflow_run_id="run-with-carrier",
        node_ids=["extract", "transform", "load"],
        carrier={"traceparent": f"  {VALID_TRACEPARENT}  ", "tracestate": "tenant=a"},
    )
    assert isinstance(parent, RecordedSpan)
    assert parent.parent_context is not None
    assert parent.parent_context.span_id == CARRIER_SPAN_ID
    assert parent.span_id != CARRIER_SPAN_ID
    assert parent.trace_id == CARRIER_TRACE_ID

    assert len(children) == 3
    for child in children:
        assert isinstance(child, RecordedSpan)
        assert child.parent_context is not None
        assert child.parent_context.trace_id == parent.trace_id
        assert child.parent_context.span_id == parent.span_id
        assert child.parent_context.span_id != CARRIER_SPAN_ID
        assert child.parent_context.tracestate == "tenant=a"
        assert child.trace_id == parent.trace_id

    # Without an external carrier, child spans must still attach to the workflow root span.
    root_parent, root_children = coord.trace_workflow_execution(
        authority=authority,
        workflow_run_id="run-root",
        node_ids=["step-a", "step-b"],
        carrier=None,
    )
    assert isinstance(root_parent, RecordedSpan)
    assert root_parent.parent_context is None
    for child in root_children:
        assert isinstance(child, RecordedSpan)
        assert child.parent_context is not None
        assert child.parent_context.trace_id == root_parent.trace_id
        assert child.parent_context.span_id == root_parent.span_id


def test_otel_tracer_real_sdk_exports_parent_child_workflow_spans_and_redacts() -> None:
    provider, exporter, otel_tracer = _make_sdk_tracer()
    try:
        assert otel_tracer.available is True
        coord = TaskTracingCoordinator(
            TracingConfig(enabled=True, max_workflow_node_spans_per_run=3),
            tracer=otel_tracer,
        )
        authority = AttemptAuthorityToken(
            task_execution_pk=99, attempt_number=2, execution_generation=1
        )
        coord.trace_workflow_execution(
            authority=authority,
            workflow_run_id="wf-otel-1",
            node_ids=["n0", "n1", "n2", "n3", "n4"],
            carrier={"traceparent": f"  {VALID_TRACEPARENT}\n", "tracestate": "vendor=ok"},
        )

        finished = exporter.get_finished_spans()
        workflow_spans = [s for s in finished if s.name == SPAN_WORKFLOW_EXECUTE]
        node_spans = [s for s in finished if s.name == SPAN_WORKFLOW_NODE]
        assert len(workflow_spans) == 1
        assert len(node_spans) == 3

        wf_span = workflow_spans[0]
        assert wf_span.parent is not None
        assert wf_span.parent.span_id == int(CARRIER_SPAN_ID, 16)
        assert wf_span.context is not None
        assert wf_span.context.trace_id == int(CARRIER_TRACE_ID, 16)
        assert wf_span.status.status_code == StatusCode.OK
        assert wf_span.attributes is not None
        assert wf_span.attributes["django_ray.workflow.node_count"] == 5
        assert wf_span.attributes["django_ray.workflow.sampled_node_spans"] == 3
        assert wf_span.attributes["django_ray.workflow.omitted_node_spans"] == 2

        for node_span in node_spans:
            assert node_span.parent is not None
            assert node_span.context is not None
            assert node_span.context.trace_id == wf_span.context.trace_id
            assert node_span.parent.span_id == wf_span.context.span_id
            assert node_span.parent.span_id != int(CARRIER_SPAN_ID, 16)
            assert node_span.status.status_code == StatusCode.OK

        # Verify attribute redaction and error status on real exported spans.
        exporter.clear()
        stage_span = coord.trace_lifecycle_stage(
            SPAN_TASK_ENQUEUE,
            authority=authority,
            carrier={"traceparent": VALID_TRACEPARENT},
            task_id="task-otel-redact",
            queue_name="gpu",
            state="QUEUED",
            extra_attributes={
                "args": "[1, 2]",
                "kwargs_json": '{"password": "secret"}',
                "runtime_env": '{"env_vars": {"API_TOKEN": "xyz"}}',
                "error_traceback": "Traceback ...",
            },
        )
        coord.safe_end_span(stage_span, error=RuntimeError("password=leaked-in-exc"))

        exported = exporter.get_finished_spans()
        assert len(exported) == 1
        span = exported[0]
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description == "RuntimeError"
        assert span.attributes is not None
        assert span.attributes["django_ray.task.id"] == "task-otel-redact"
        assert span.attributes["django_ray.error.type"] == "RuntimeError"
        assert all(k.startswith("django_ray.") for k in span.attributes)
        assert "leaked-in-exc" not in str(dict(span.attributes))
    finally:
        provider.shutdown()


def test_otel_tracer_workflow_without_carrier_links_root_and_children() -> None:
    provider, exporter, otel_tracer = _make_sdk_tracer()
    try:
        coord = TaskTracingCoordinator(TracingConfig(enabled=True), tracer=otel_tracer)
        authority = AttemptAuthorityToken(
            task_execution_pk=15, attempt_number=1, execution_generation=0
        )
        coord.trace_workflow_execution(
            authority=authority,
            workflow_run_id="wf-root-only",
            node_ids=["a", "b"],
            carrier=None,
        )
        finished = exporter.get_finished_spans()
        wf_span = next(s for s in finished if s.name == SPAN_WORKFLOW_EXECUTE)
        node_spans = [s for s in finished if s.name == SPAN_WORKFLOW_NODE]
        assert wf_span.parent is None
        assert wf_span.context is not None
        assert len(node_spans) == 2
        for child in node_spans:
            assert child.parent is not None
            assert child.context is not None
            assert child.context.trace_id == wf_span.context.trace_id
            assert child.parent.span_id == wf_span.context.span_id
    finally:
        provider.shutdown()


def test_otel_tracer_graceful_noop_when_opentelemetry_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)

    tracer = OpenTelemetryTracer()
    assert tracer.available is False

    coord = TaskTracingCoordinator(TracingConfig(enabled=True), tracer=tracer)
    authority = AttemptAuthorityToken(task_execution_pk=5, attempt_number=1, execution_generation=0)

    span = coord.trace_lifecycle_stage(
        SPAN_TASK_ENQUEUE,
        authority=authority,
        carrier={"traceparent": VALID_TRACEPARENT},
        task_id="task-noop",
    )
    coord.safe_end_span(span)
    parent, children = coord.trace_workflow_execution(
        authority=authority,
        workflow_run_id="wf-noop",
        node_ids=["n1", "n2"],
    )
    assert len(children) == 2
    assert coord.diagnostics.exporter_error_count == 0


def test_emit_task_lifecycle_span_disabled_enabled_and_exception_isolated() -> None:
    provider, exporter, otel_tracer = _make_sdk_tracer()
    previous = get_task_tracing_coordinator()
    try:
        configure_task_tracing(None)
        execution = SimpleNamespace(
            pk=42,
            task_id="exec-42",
            attempt_number=1,
            execution_generation=0,
            queue_name="default",
            state="QUEUED",
            runner_mode="ray_core",
            trace_context={"traceparent": f" {VALID_TRACEPARENT} "},
        )
        # Disabled by default -> strict no-op.
        emit_task_lifecycle_span("enqueued", execution)
        emit_task_lifecycle_span("started", execution)
        emit_task_lifecycle_span("finished", execution)
        assert not exporter.get_finished_spans()

        # Opt-in activation via coordinator with real OTel SDK.
        coord = TaskTracingCoordinator(TracingConfig(enabled=True), tracer=otel_tracer)
        configure_task_tracing(coord)

        emit_task_lifecycle_span("enqueued", execution)
        execution.state = "RUNNING"
        emit_task_lifecycle_span("started", execution)
        execution.state = "SUCCEEDED"
        emit_task_lifecycle_span("finished", execution)

        failed_execution = SimpleNamespace(
            pk=43,
            task_id="exec-43",
            attempt_number=2,
            execution_generation=1,
            queue_name="default",
            state="FAILED",
            error_type="ValueError",
            error_message="super-secret-db-password",
            cancellation_status="NONE",
        )
        emit_task_lifecycle_span("finished", failed_execution)

        spans = exporter.get_finished_spans()
        assert [s.name for s in spans] == [
            SPAN_TASK_ENQUEUE,
            SPAN_TASK_REMOTE_EXECUTE,
            SPAN_TASK_TERMINAL_PERSIST,
            SPAN_TASK_TERMINAL_PERSIST,
        ]
        assert spans[0].parent is not None
        assert spans[0].parent.span_id == int(CARRIER_SPAN_ID, 16)
        assert spans[2].status.status_code == StatusCode.OK
        assert spans[2].attributes is not None
        assert spans[2].attributes["django_ray.attempt.authoritative"] is True

        failed_span = spans[3]
        assert failed_span.status.status_code == StatusCode.ERROR
        assert failed_span.status.description == "ValueError"
        assert failed_span.attributes is not None
        assert failed_span.attributes["django_ray.error.type"] == "ValueError"
        assert "super-secret-db-password" not in str(dict(failed_span.attributes))

        # Invalid/broken inputs and failing tracers must never raise.
        class ExplodingExecution:
            @property
            def pk(self) -> int:
                raise RuntimeError("db disconnected")

        class BrokenTracer:
            def start_span(self, name: str, **kwargs: Any) -> Any:
                raise RuntimeError("exporter down")

        emit_task_lifecycle_span("enqueued", ExplodingExecution())
        emit_task_lifecycle_span("enqueued", SimpleNamespace(pk=None))
        emit_task_lifecycle_span("unknown", execution)  # type: ignore[arg-type]

        configure_task_tracing(
            TaskTracingCoordinator(TracingConfig(enabled=True), tracer=BrokenTracer())
        )
        emit_task_lifecycle_span("enqueued", execution)
        emit_task_lifecycle_span("finished", failed_execution)

        # Verify default global tracer provider + UNSET status + Django settings opt-in.
        default_tracer = OpenTelemetryTracer()
        default_span = default_tracer.start_span(SPAN_TASK_ENQUEUE)
        default_span.set_status("UNSET")
        default_span.end()

        configure_task_tracing(None)
        fake_conf = SimpleNamespace(
            settings=SimpleNamespace(
                configured=True,
                DJANGO_RAY={"TRACING_ENABLED": True, "TRACING_SAMPLE_RATE": 1.0},
            )
        )
        prev_django_conf = sys.modules.get("django.conf")
        sys.modules["django.conf"] = fake_conf  # type: ignore[assignment]
        try:
            emit_task_lifecycle_span("enqueued", execution)
        finally:
            if prev_django_conf is None:
                sys.modules.pop("django.conf", None)
            else:
                sys.modules["django.conf"] = prev_django_conf
    finally:
        configure_task_tracing(previous)
        provider.shutdown()


def test_pyproject_declares_otel_extra_and_keeps_transitive_security_floor() -> None:
    pyproject_path = Path(__file__).parents[2] / "pyproject.toml"
    config = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    optional_deps = config["project"]["optional-dependencies"]
    assert "otel" in optional_deps
    assert any(req.startswith("opentelemetry-sdk>=") for req in optional_deps["otel"])
    assert any(req.startswith("opentelemetry-api>=") for req in config["project"]["dependencies"])
