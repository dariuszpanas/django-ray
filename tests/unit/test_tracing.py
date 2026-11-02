"""Unit tests for optional OpenTelemetry task and workflow correlation (#369)."""

from __future__ import annotations

import time
from typing import Any

from django_ray.tracing import (
    SPAN_TASK_CLAIM,
    SPAN_TASK_ENQUEUE,
    SPAN_TASK_RAY_SUBMIT,
    SPAN_TASK_REMOTE_EXECUTE,
    SPAN_TASK_RETRY_RECONCILE,
    SPAN_TASK_TERMINAL_PERSIST,
    SPAN_WORKFLOW_EXECUTE,
    SPAN_WORKFLOW_NODE,
    AttemptAuthorityToken,
    InMemoryTracer,
    TaskTracingCoordinator,
    TraceContextEnvelope,
    TracingConfig,
    parse_trace_context,
    sanitize_span_attributes,
)

VALID_TRACEPARENT = "00-11112222333344445555666677778888-1111222233334444-01"


def test_w3c_trace_context_parsing_and_redaction() -> None:
    res = parse_trace_context({"traceparent": VALID_TRACEPARENT, "tracestate": "vendor=ok,tier=1"})
    assert res.status == "ok"
    assert res.envelope is not None
    assert res.envelope.trace_id == "11112222333344445555666677778888"
    assert res.envelope.span_id == "1111222233334444"
    assert res.envelope.to_carrier() == {
        "traceparent": VALID_TRACEPARENT,
        "tracestate": "vendor=ok,tier=1",
    }

    # Malformed traceparent fails open without raising.
    for bad in [
        None,
        {},
        {"traceparent": "bad"},
        {"traceparent": "ff-11112222333344445555666677778888-1111222233334444-01"},
        {"traceparent": f"00-{'0' * 32}-1111222233334444-01"},
        {"traceparent": f"00-11112222333344445555666677778888-{'0' * 16}-01"},
    ]:
        parsed = parse_trace_context(bad)
        assert parsed.envelope is None

    # Secret or oversized tracestate preserves valid traceparent and strips tracestate.
    redacted_ts = parse_trace_context(
        {"traceparent": VALID_TRACEPARENT, "tracestate": "api_secret=topsecret"}
    )
    assert redacted_ts.status == "redacted_tracestate"
    assert redacted_ts.envelope is not None
    assert redacted_ts.envelope.tracestate == ""

    oversized_ts = parse_trace_context(
        {"traceparent": VALID_TRACEPARENT, "tracestate": "k=" + "v" * 600}
    )
    assert oversized_ts.status == "oversized_tracestate"
    assert oversized_ts.envelope is not None
    assert oversized_ts.envelope.tracestate == ""


def test_attribute_redaction_strips_args_kwargs_results_runtime_env_and_secrets() -> None:
    tracer = InMemoryTracer()
    coord = TaskTracingCoordinator(TracingConfig(enabled=True), tracer=tracer)
    authority = AttemptAuthorityToken(
        task_execution_pk=10, attempt_number=1, execution_generation=0
    )

    span = coord.trace_lifecycle_stage(
        SPAN_TASK_ENQUEUE,
        authority=authority,
        carrier={"traceparent": VALID_TRACEPARENT},
        task_id="task-369",
        queue_name="default",
        state="QUEUED",
        runner_mode="ray_core",
        extra_attributes={
            "args": "[1, 2, 3]",
            "kwargs_json": '{"secret": "leak"}',
            "result_data": "sensitive-output",
            "runtime_env": '{"env_vars": {"TOKEN": "x"}}',
            "error_message": "raw message",
            "error_traceback": "Traceback ...",
            "api_token": "bearer-xyz",
        },
    )
    coord.safe_end_span(span, error=RuntimeError("db password leaked in exception text"))

    assert len(tracer.spans) == 1
    recorded = tracer.spans[0]
    assert recorded.ended is True
    assert recorded.attributes["django_ray.task.id"] == "task-369"
    assert recorded.attributes["django_ray.task.execution_pk"] == 10
    assert recorded.attributes["django_ray.error.type"] == "RuntimeError"
    assert recorded.status_description == "RuntimeError"
    # Verify no forbidden payload/secret/traceback attributes leaked.
    for key in recorded.attributes:
        assert key.startswith("django_ray.")
    assert "db password" not in str(recorded.attributes)
    assert coord.diagnostics.redacted_attribute_count == 7


def test_stale_attempt_and_lease_loss_fencing_blocks_authoritative_terminal_span() -> None:
    tracer = InMemoryTracer()
    coord = TaskTracingCoordinator(TracingConfig(enabled=True), tracer=tracer)
    stale_attempt = AttemptAuthorityToken(
        task_execution_pk=42,
        attempt_number=1,
        execution_generation=1,
    )
    current_db = AttemptAuthorityToken(
        task_execution_pk=42,
        attempt_number=2,
        execution_generation=2,
    )

    # Stale generation/attempt cannot emit SPAN_TASK_TERMINAL_PERSIST.
    is_auth, fenced_span = coord.record_terminal_persist(
        attempt_authority=stale_attempt,
        current_db_authority=current_db,
        lease_owned=True,
        terminal_state="SUCCEEDED",
        carrier={"traceparent": VALID_TRACEPARENT},
    )
    assert is_auth is False
    assert coord.diagnostics.fenced_stale_terminal_count == 1
    assert tracer.spans[-1].name == SPAN_TASK_RETRY_RECONCILE
    assert tracer.spans[-1].attributes["django_ray.attempt.authoritative"] is False
    assert tracer.spans[-1].attributes["django_ray.attempt.superseded_reason"] == "stale_generation"

    # Lease loss on matching generation is also fenced.
    is_auth_lease, _ = coord.record_terminal_persist(
        attempt_authority=current_db,
        current_db_authority=current_db,
        lease_owned=False,
        terminal_state="SUCCEEDED",
    )
    assert is_auth_lease is False
    assert coord.diagnostics.fenced_stale_terminal_count == 2
    assert tracer.spans[-1].attributes["django_ray.attempt.superseded_reason"] == "lease_lost"

    # Current owner with active lease emits authoritative terminal span.
    is_auth_ok, term_span = coord.record_terminal_persist(
        attempt_authority=current_db,
        current_db_authority=current_db,
        lease_owned=True,
        terminal_state="CANCELLED",
        cancellation_status="CANCELLED",
    )
    assert is_auth_ok is True
    assert tracer.spans[-1].name == SPAN_TASK_TERMINAL_PERSIST
    assert tracer.spans[-1].attributes["django_ray.attempt.authoritative"] is True
    assert tracer.spans[-1].attributes["django_ray.task.cancellation_status"] == "CANCELLED"


def test_workflow_fanout_budget_caps_child_node_spans() -> None:
    tracer = InMemoryTracer()
    coord = TaskTracingCoordinator(
        TracingConfig(enabled=True, max_workflow_node_spans_per_run=5),
        tracer=tracer,
    )
    authority = AttemptAuthorityToken(
        task_execution_pk=77, attempt_number=1, execution_generation=1
    )
    node_ids = [f"node-{i:04d}" for i in range(100)]

    parent, children = coord.trace_workflow_execution(
        authority=authority,
        workflow_run_id="run-77",
        node_ids=node_ids,
        carrier={"traceparent": VALID_TRACEPARENT},
    )
    assert len(children) == 5
    assert coord.diagnostics.omitted_workflow_node_span_count == 95
    workflow_spans = [s for s in tracer.spans if s.name == SPAN_WORKFLOW_EXECUTE]
    node_spans = [s for s in tracer.spans if s.name == SPAN_WORKFLOW_NODE]
    assert len(workflow_spans) == 1
    assert len(node_spans) == 5
    assert workflow_spans[0].attributes["django_ray.workflow.node_count"] == 100
    assert workflow_spans[0].attributes["django_ray.workflow.sampled_node_spans"] == 5
    assert workflow_spans[0].attributes["django_ray.workflow.omitted_node_spans"] == 95


def test_exporter_and_tracer_failures_are_isolated_fail_open() -> None:
    class BrokenTracer:
        def start_span(
            self,
            name: str,
            *,
            parent_context: TraceContextEnvelope | None = None,
            attributes: dict[str, Any] | None = None,
        ) -> Any:
            raise RuntimeError("OTLP collector unavailable")

    coord = TaskTracingCoordinator(TracingConfig(enabled=True), tracer=BrokenTracer())
    authority = AttemptAuthorityToken(task_execution_pk=1, attempt_number=1, execution_generation=1)

    for stage in (
        SPAN_TASK_ENQUEUE,
        SPAN_TASK_CLAIM,
        SPAN_TASK_RAY_SUBMIT,
        SPAN_TASK_REMOTE_EXECUTE,
    ):
        sp = coord.trace_lifecycle_stage(stage, authority=authority)
        coord.safe_end_span(sp)

    assert coord.diagnostics.exporter_error_count == 4
    assert coord.diagnostics.spans_started == 0


def test_disabled_tracing_overhead_is_noop() -> None:
    coord = TaskTracingCoordinator(TracingConfig(enabled=False), tracer=None)
    authority = AttemptAuthorityToken(task_execution_pk=9, attempt_number=1, execution_generation=1)

    start = time.perf_counter()
    for _ in range(500):
        sp = coord.trace_lifecycle_stage(SPAN_TASK_ENQUEUE, authority=authority)
        coord.safe_end_span(sp)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5
    assert coord.diagnostics.spans_started == 0
    assert len(sanitize_span_attributes(None)) == 0


def test_tracing_config_tracestate_and_coordinator_edge_cases() -> None:
    import pytest

    from django_ray.tracing import _NOOP_SPAN

    with pytest.raises(ValueError, match="AttemptAuthorityToken"):
        AttemptAuthorityToken(0, 1, 1).validate()
    with pytest.raises(ValueError, match="enabled must be a bool"):
        TracingConfig(enabled="yes").validated()  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sample_rate must be a float"):
        TracingConfig(sample_rate=True).validated()  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        TracingConfig(sample_rate=1.5).validated()
    with pytest.raises(ValueError, match="max_workflow_node_spans_per_run"):
        TracingConfig(max_workflow_node_spans_per_run=999).validated()

    assert parse_trace_context("not-dict").status == "malformed_traceparent"  # type: ignore[arg-type]
    assert parse_trace_context({"other": "1"}).status == "missing_context"
    assert (
        parse_trace_context(
            {
                "traceparent": VALID_TRACEPARENT,
                "tracestate": 123,
            }
        ).status
        == "malformed_tracestate"
    )
    assert (
        parse_trace_context(
            {
                "traceparent": VALID_TRACEPARENT,
                "tracestate": "k=v\n",
            }
        ).status
        == "malformed_tracestate"
    )
    assert (
        parse_trace_context(
            {
                "traceparent": VALID_TRACEPARENT,
                "tracestate": ",".join(f"k{i}=v" for i in range(35)),
            }
        ).status
        == "oversized_tracestate"
    )
    assert (
        parse_trace_context(
            {
                "traceparent": VALID_TRACEPARENT,
                "tracestate": "noequals",
            }
        ).status
        == "malformed_tracestate"
    )
    assert (
        parse_trace_context(
            {
                "traceparent": VALID_TRACEPARENT,
                "tracestate": "k=",
            }
        ).status
        == "malformed_tracestate"
    )

    _NOOP_SPAN.set_attribute("django_ray.task.id", "1")
    _NOOP_SPAN.set_status("OK")
    _NOOP_SPAN.end()

    tracer = InMemoryTracer()
    coord_zero = TaskTracingCoordinator(
        TracingConfig(enabled=True, sample_rate=0.0),
        tracer=tracer,
    )
    auth = AttemptAuthorityToken(1, 1, 1)
    assert coord_zero.trace_lifecycle_stage(SPAN_TASK_ENQUEUE, authority=auth) is _NOOP_SPAN

    coord_half = TaskTracingCoordinator(
        TracingConfig(enabled=True, sample_rate=0.5),
        tracer=tracer,
    )
    assert coord_half.extract_context({"traceparent": "bad"}) is None
    assert coord_half.diagnostics.malformed_context_count == 1
    with pytest.raises(ValueError, match="Unsupported django-ray span name"):
        coord_half.trace_lifecycle_stage("bad.span", authority=auth)

    coord_full = TaskTracingCoordinator(TracingConfig(enabled=True), tracer=tracer)
    sp = coord_full.trace_lifecycle_stage(
        SPAN_TASK_ENQUEUE,
        authority=auth,
        carrier={"traceparent": "bad"},
        extra_attributes={123: "bad", "django_ray.task.id": object()},  # type: ignore[dict-item]
    )
    coord_full.safe_end_span(sp, extra_attributes={"django_ray.task.state": "QUEUED"})
    with pytest.raises(ValueError, match="same task_execution_pk"):
        coord_full.record_terminal_persist(
            attempt_authority=auth,
            current_db_authority=AttemptAuthorityToken(2, 1, 1),
            lease_owned=True,
            terminal_state="SUCCEEDED",
        )
