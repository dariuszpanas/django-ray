"""Tests for workflow graph and Ray live-observability helpers."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

import django_ray.observability as observability_module
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.observability import (
    DEFAULT_DIAGNOSTIC_MAX_CHARS,
    MAX_RAY_LOG_MAX_BYTES,
    WorkflowObservabilityError,
    get_attempt_history,
    get_queue_depths,
    get_ray_task_logs,
    get_ray_task_state,
    get_task_summary,
    get_workflow_graph,
    get_workflow_node,
    get_workflow_node_snapshot,
    get_workflow_plan,
    get_workflow_plan_diagnostics,
    get_workflow_progress,
    get_workflow_snapshot,
)
from django_ray.workflow_plans import (
    PLAN_DOMAIN_SEPARATOR,
    EffectiveWorkflowPlan,
    PlanEligibility,
    PlanRejection,
    materialize_workflow_plan,
)
from django_ray.workflows import map_step


@pytest.fixture
def workflow_execution(db) -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id="workflow-observability-1",
        callable_path="testproject.tasks.workflow",
        attempt_number=2,
        execution_generation=4,
        workflow_run_id="00000000-0000-0000-0000-000000000031",
        progress_data=json.dumps(
            {
                "schema_version": 1,
                "revision": 3,
                "graph": {
                    "nodes": [
                        {
                            "node_id": "0.0",
                            "dependencies": [],
                            "execution": {"ray_task_id": "ray-task-1"},
                        },
                        {
                            "node_id": "0.1",
                            "dependencies": ["0.0"],
                            "execution": {},
                        },
                    ],
                    "edges": [{"source": "0.0", "target": "0.1"}],
                },
            }
        ),
    )


def _workflow_plan_fingerprint(serialized: str) -> str:
    encoded = serialized.encode("utf-8")
    digest = hashlib.sha256(PLAN_DOMAIN_SEPARATOR + encoded).hexdigest()
    return f"sha256:{digest}"


def _diagnostic_increment(value: int) -> int:
    return value + 1


@pytest.fixture(scope="module")
def diagnostic_workflow_plan() -> EffectiveWorkflowPlan:
    return materialize_workflow_plan(
        map_step(_diagnostic_increment),
        invocation_args=([1, 2],),
    ).plan


def test_get_workflow_graph_and_node(workflow_execution) -> None:
    graph = get_workflow_graph(workflow_execution)
    node = get_workflow_node(workflow_execution, "0.0")

    assert graph is not None
    assert node is not None
    assert graph["edges"] == [{"source": "0.0", "target": "0.1"}]
    assert node["execution"] == {"ray_task_id": "ray-task-1"}
    assert get_workflow_node(workflow_execution, "missing") is None


def test_versioned_task_summary_omits_sensitive_payloads(db, settings) -> None:
    settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"customer[_-]?email"]}
    generated_at = datetime(2026, 7, 19, 12, tzinfo=UTC)
    execution = RayTaskExecution.objects.create(
        task_id="task-summary-1",
        callable_path="testproject.tasks.echo",
        queue_name="critical",
        priority=75,
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=4,
        workflow_run_id="00000000-0000-0000-0000-000000000032",
        started_at=generated_at - timedelta(seconds=3),
        last_heartbeat_at=generated_at - timedelta(seconds=1),
        claimed_by_worker="worker-1",
        ray_job_id="ray-job-1",
        ray_address="ray://private-host:10001",
        runtime_env_profile="numpy",
        runtime_env_hash="abc123",
        workflow_plan_pinned_attempt=1,
        runtime_env_json='{"password":"secret"}',
        args_json='["secret"]',
        kwargs_json='{"token":"secret"}',
        result_data='{"private":"secret"}',
        input_reference="s3://private/input",
        result_reference="s3://private/result",
        error_message="customer_email=private@example.com",
        error_traceback="private traceback",
        progress_data=json.dumps({"revision": 7}),
    )

    summary = get_task_summary(execution, generated_at=generated_at)

    assert summary["schema"] == "django-ray.task-summary"
    assert summary["schema_version"] == 1
    assert summary["generated_at"] == "2026-07-19T12:00:00Z"
    assert summary["workflow_revision"] == 7
    assert summary["workflow_run_id"] == "00000000-0000-0000-0000-000000000032"
    assert summary["workflow_plan_pinned_attempt"] == 1
    assert summary["workflow_reporting_policy"] is None
    assert summary["error_message"] == "[REDACTED]"
    assert summary["error_message_truncated"] is False
    assert summary["started_at"] == "2026-07-19T11:59:57Z"
    assert {
        "args_json",
        "kwargs_json",
        "result_data",
        "error_traceback",
        "ray_address",
        "runtime_env_json",
        "input_reference",
        "result_reference",
    }.isdisjoint(summary)

    execution.progress_data = "{"
    execution.error_message = None
    execution.save(update_fields=["progress_data", "error_message"])
    assert get_task_summary(execution)["workflow_revision"] is None
    execution.progress_data = json.dumps({"revision": "not-an-integer"})
    execution.save(update_fields=["progress_data"])
    assert get_task_summary(execution)["workflow_revision"] is None


@pytest.mark.parametrize("selection", ["{", "[]"])
def test_task_summary_omits_invalid_plan_selection(db, selection: str) -> None:
    execution = RayTaskExecution.objects.create(
        task_id=f"task-invalid-selection-{len(selection)}",
        callable_path="testproject.tasks.echo",
        workflow_plan_selection=selection,
    )

    summary = get_task_summary(execution)

    assert summary["workflow_selected_strategy"] is None
    assert summary["workflow_reporting_policy"] is None


def test_task_summary_redacts_selection_fields_after_validation(db, settings) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "REDACT_PATTERNS": [r"disabled"],
    }
    selection = PlanEligibility(("dynamic_tasks",), (), 0).select(
        "dynamic_tasks",
        requested_policy="auto",
        reporting_policy="disabled",
    )
    execution = RayTaskExecution.objects.create(
        task_id="task-redacted-selection",
        callable_path="testproject.tasks.echo",
        workflow_plan_selection=json.dumps(selection.as_dict()),
    )

    summary = get_task_summary(execution)

    assert summary["workflow_selected_strategy"] == "dynamic_tasks"
    assert summary["workflow_reporting_policy"] == "[REDACTED]"


def test_task_summary_contains_deeply_nested_plan_selection_failure(db, monkeypatch) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="task-recursive-selection",
        callable_path="testproject.tasks.echo",
        workflow_plan_selection="{}",
    )
    original_loads = observability_module.json.loads

    def recursive_selection_loads(value):
        if value == execution.workflow_plan_selection:
            raise RecursionError
        return original_loads(value)

    monkeypatch.setattr(observability_module.json, "loads", recursive_selection_loads)

    summary = get_task_summary(execution)

    assert summary["workflow_selected_strategy"] is None
    assert summary["workflow_reporting_policy"] is None


def test_get_workflow_plan_rejects_incomplete_snapshot(db) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-incomplete-plan",
        callable_path="testproject.tasks.workflow",
        workflow_plan_json="{}",
    )

    with pytest.raises(WorkflowObservabilityError, match="incomplete workflow plan snapshot"):
        get_workflow_plan(execution)


def test_workflow_plan_diagnostics_summarizes_real_dynamic_plan_without_raw_rejections(
    db,
    diagnostic_workflow_plan,
) -> None:
    selection = PlanEligibility(
        ("dynamic_tasks", "local"),
        (
            PlanRejection(
                "compiled_graph",
                "UNRESOLVED_CODE_IDENTITY",
                "private.first.path",
                "private first message",
            ),
            PlanRejection(
                "compiled_graph",
                "UNRESOLVED_CODE_IDENTITY",
                "private.second.path",
                "private second message",
            ),
            PlanRejection(
                "static_actors",
                "UNSUPPORTED_NODE_MODEL",
                "private.third.path",
                "private third message",
            ),
        ),
        4,
    ).select(
        "dynamic_tasks",
        requested_policy="auto",
        reporting_policy="full",
    )
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-diagnostics-valid",
        callable_path="tests.unit.test_observability._diagnostic_increment",
        workflow_plan_fingerprint=diagnostic_workflow_plan.fingerprint,
        workflow_plan_json=diagnostic_workflow_plan.canonical_json,
        workflow_plan_selection=json.dumps(selection.as_dict()),
    )

    diagnostics = get_workflow_plan_diagnostics(execution)

    plan_summary = diagnostic_workflow_plan.summary()
    assert diagnostics == {
        "status": "AVAILABLE",
        "definition_name": plan_summary["definition_name"],
        "definition_revision": plan_summary["definition_revision"],
        "topology_class": "dynamic",
        "declared_node_count": plan_summary["node_count"],
        "retry_safe": diagnostic_workflow_plan.retry_safe,
        "fingerprint": diagnostic_workflow_plan.fingerprint,
        "fingerprint_compact": (
            f"sha256:{diagnostic_workflow_plan.fingerprint.removeprefix('sha256:')[:12]}"
        ),
        "requested_policy": "auto",
        "selected_strategy": "dynamic_tasks",
        "reporting_policy": "full",
        "eligible_strategies": ["dynamic_tasks", "local"],
        "rejection_counts": {
            "UNRESOLVED_CODE_IDENTITY": 2,
            "UNSUPPORTED_NODE_MODEL": 1,
        },
        "retained_rejections": 3,
        "total_rejections": 4,
        "unretained_rejections": 1,
    }
    rendered = json.dumps(diagnostics)
    assert "private.first.path" not in rendered
    assert "private second message" not in rendered
    assert '"path"' not in rendered
    assert '"message"' not in rendered


def test_workflow_plan_diagnostics_redacts_rejection_codes_after_validation(
    db,
    settings,
    diagnostic_workflow_plan,
) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "REDACT_PATTERNS": [r"UNRESOLVED_CODE_IDENTITY"],
    }
    selection = PlanEligibility(
        ("dynamic_tasks", "local"),
        (
            PlanRejection(
                "compiled_graph",
                "UNRESOLVED_CODE_IDENTITY",
                "private.path",
                "private message",
            ),
        ),
        1,
    ).select(
        "dynamic_tasks",
        requested_policy="auto",
        reporting_policy="full",
    )
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-diagnostics-redacted-code",
        callable_path="tests.unit.test_observability._diagnostic_increment",
        workflow_plan_fingerprint=diagnostic_workflow_plan.fingerprint,
        workflow_plan_json=diagnostic_workflow_plan.canonical_json,
        workflow_plan_selection=json.dumps(selection.as_dict()),
    )

    diagnostics = get_workflow_plan_diagnostics(execution)

    assert diagnostics["status"] == "AVAILABLE"
    assert diagnostics["rejection_counts"] == {"[REDACTED]": 1}
    assert "UNRESOLVED_CODE_IDENTITY" not in json.dumps(diagnostics)


def test_workflow_plan_diagnostics_reports_not_recorded_with_fixed_shape(db) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-plan-diagnostics-none",
        callable_path="tests.unit.test_observability._diagnostic_increment",
    )

    diagnostics = get_workflow_plan_diagnostics(execution)

    assert diagnostics["status"] == "NOT_RECORDED"
    assert diagnostics["eligible_strategies"] == []
    assert diagnostics["rejection_counts"] == {}
    assert diagnostics["fingerprint"] is None
    assert diagnostics["declared_node_count"] is None


@pytest.mark.parametrize(
    "case",
    [
        "plan_only",
        "selection_only",
        "malformed_plan",
        "fingerprint_mismatch",
        "invalid_selection",
        "missing_summary_fields",
    ],
)
def test_workflow_plan_diagnostics_fail_closed_for_unverified_snapshots(
    db,
    diagnostic_workflow_plan,
    case: str,
) -> None:
    selection = diagnostic_workflow_plan.eligibility.select(
        "dynamic_tasks",
        requested_policy="auto",
    )
    serialized_plan = diagnostic_workflow_plan.canonical_json
    fingerprint = diagnostic_workflow_plan.fingerprint
    serialized_selection = json.dumps(selection.as_dict())
    if case == "plan_only":
        serialized_selection = None
    elif case == "selection_only":
        serialized_plan = None
        fingerprint = None
    elif case == "malformed_plan":
        serialized_plan = '{"private":"plan-secret"'
        fingerprint = _workflow_plan_fingerprint(serialized_plan)
    elif case == "fingerprint_mismatch":
        fingerprint = "sha256:" + ("0" * 64)
    elif case == "invalid_selection":
        serialized_selection = '{"private":"selection-secret"}'
    elif case == "missing_summary_fields":
        serialized_plan = json.dumps(
            {
                "plan_format": "django-ray.workflow-plan",
                "plan_format_version": 1,
                "private": "missing-summary-secret",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        fingerprint = _workflow_plan_fingerprint(serialized_plan)
    execution = RayTaskExecution.objects.create(
        task_id=f"workflow-plan-diagnostics-{case}",
        callable_path="tests.unit.test_observability._diagnostic_increment",
        workflow_plan_fingerprint=fingerprint,
        workflow_plan_json=serialized_plan,
        workflow_plan_selection=serialized_selection,
    )

    with pytest.raises(WorkflowObservabilityError) as raised:
        get_workflow_plan_diagnostics(execution)

    error = str(raised.value)
    assert "plan-secret" not in error
    assert "selection-secret" not in error
    assert "missing-summary-secret" not in error


@pytest.mark.parametrize(
    ("serialized", "message"),
    [
        ("{", "invalid workflow plan JSON"),
        ("[]", "workflow plan must be a JSON object"),
        (
            json.dumps(
                {
                    "plan_format": "django-ray.workflow-plan",
                    "plan_format_version": 999,
                }
            ),
            "unsupported format version",
        ),
    ],
)
def test_get_workflow_plan_rejects_invalid_verified_manifest(
    db,
    serialized: str,
    message: str,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id=f"workflow-invalid-plan-{len(serialized)}",
        callable_path="testproject.tasks.workflow",
        workflow_plan_fingerprint=_workflow_plan_fingerprint(serialized),
        workflow_plan_json=serialized,
    )

    with pytest.raises(WorkflowObservabilityError, match=message):
        get_workflow_plan(execution)


def test_queue_depths_distinguish_ready_delayed_and_running(db) -> None:
    observed_at = datetime(2026, 7, 19, 12, tzinfo=UTC)
    RayTaskExecution.objects.bulk_create(
        [
            RayTaskExecution(
                task_id="ready-1",
                callable_path="tasks.echo",
                queue_name="alpha",
                state=TaskState.QUEUED,
            ),
            RayTaskExecution(
                task_id="delayed-1",
                callable_path="tasks.echo",
                queue_name="alpha",
                state=TaskState.QUEUED,
                run_after=observed_at + timedelta(minutes=5),
            ),
            RayTaskExecution(
                task_id="running-1",
                callable_path="tasks.echo",
                queue_name="alpha",
                state=TaskState.RUNNING,
            ),
            RayTaskExecution(
                task_id="ready-2",
                callable_path="tasks.echo",
                queue_name="beta",
                state=TaskState.QUEUED,
                run_after=observed_at,
            ),
            RayTaskExecution(
                task_id="ignored",
                callable_path="tasks.echo",
                queue_name="beta",
                state=TaskState.SUCCEEDED,
            ),
        ]
    )

    snapshot = get_queue_depths(generated_at=observed_at)

    assert snapshot["schema"] == "django-ray.queue-depths"
    assert snapshot["queues"] == [
        {
            "queue_name": "alpha",
            "queued": 2,
            "ready": 1,
            "delayed": 1,
            "running": 1,
            "oldest_queued_at": snapshot["queues"][0]["oldest_queued_at"],
        },
        {
            "queue_name": "beta",
            "queued": 1,
            "ready": 1,
            "delayed": 0,
            "running": 0,
            "oldest_queued_at": snapshot["queues"][1]["oldest_queued_at"],
        },
    ]
    assert all(queue["oldest_queued_at"].endswith("Z") for queue in snapshot["queues"])


def test_attempt_history_adds_current_snapshot_and_deduplicates_archive(db, settings) -> None:
    settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"access[_-]?token"]}
    started_at = datetime(2026, 7, 19, 12, tzinfo=UTC)
    execution = RayTaskExecution.objects.create(
        task_id="attempt-history-1",
        callable_path="tasks.echo",
        state=TaskState.RUNNING,
        attempt_number=2,
        started_at=started_at,
    )
    TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.FAILED,
        started_at=started_at - timedelta(seconds=10),
        finished_at=started_at - timedelta(seconds=15),
        error_message="access_token=secret-value",
    )

    history = get_attempt_history(execution, generated_at=started_at)

    assert history["schema"] == "django-ray.attempt-history"
    assert [attempt["attempt_number"] for attempt in history["attempts"]] == [1, 2]
    assert history["attempts"][0]["duration_seconds"] == 0.0
    assert history["attempts"][0]["error_message"] == "[REDACTED]"
    assert history["attempts"][0]["error_message_truncated"] is False
    assert history["attempts"][1]["current"] is True
    assert history["attempts"][1]["duration_seconds"] is None

    TaskAttempt.objects.create(
        execution=execution,
        attempt_number=2,
        state=TaskState.SUCCEEDED,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=2),
    )
    history = get_attempt_history(execution)
    assert len(history["attempts"]) == 2
    assert history["attempts"][1]["current"] is False
    assert history["attempts"][1]["duration_seconds"] == 2.0


def test_diagnostic_messages_are_bounded_with_explicit_metadata(db) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="bounded-diagnostics-1",
        callable_path="tasks.fail",
        state=TaskState.FAILED,
        error_message="x" * (DEFAULT_DIAGNOSTIC_MAX_CHARS + 100),
    )

    summary = get_task_summary(execution)
    history = get_attempt_history(execution)

    assert len(summary["error_message"]) == DEFAULT_DIAGNOSTIC_MAX_CHARS
    assert summary["error_message"].endswith("... [truncated]")
    assert summary["error_message_truncated"] is True
    assert history["attempts"][0]["error_message_truncated"] is True


def test_workflow_snapshot_wraps_present_and_absent_progress(workflow_execution, db) -> None:
    snapshot = get_workflow_snapshot(workflow_execution)
    assert snapshot["schema"] == "django-ray.workflow-snapshot"
    assert snapshot["attempt_number"] == 2
    assert snapshot["execution_generation"] == 4
    assert snapshot["workflow_run_id"] == "00000000-0000-0000-0000-000000000031"
    assert snapshot["workflow"]["revision"] == 3

    execution = RayTaskExecution.objects.create(
        task_id="no-workflow",
        callable_path="tasks.echo",
    )
    assert get_workflow_snapshot(execution)["workflow"] is None


def test_get_ray_task_state_serializes_attempts(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265",
    }
    fake_state = SimpleNamespace(
        get_task=lambda **kwargs: [
            SimpleNamespace(asdict=lambda: {"task_id": kwargs["id"], "state": "RUNNING"})
        ],
        get_log=lambda **kwargs: [],
    )
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    assert get_ray_task_state("ray-task-1") == [{"task_id": "ray-task-1", "state": "RUNNING"}]


def test_get_ray_task_logs_returns_bounded_streams(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265",
    }
    fake_state = SimpleNamespace(
        get_task=lambda **kwargs: None,
        get_log=lambda **kwargs: iter([f"{kwargs['suffix']}-line\n"]),
    )
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    assert get_ray_task_logs("ray-task-1", tail=20) == {
        "out": "out-line\n",
        "err": "err-line\n",
    }


def test_get_ray_task_logs_enforces_utf8_byte_limit(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {"RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265"}
    fake_state = SimpleNamespace(
        get_task=lambda **kwargs: None,
        get_log=lambda **kwargs: iter(["ab", "cdef"]),
    )
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    assert get_ray_task_logs("ray-task-1", max_bytes=4) == {"out": "[TRU", "err": "[TRU"}

    fake_state.get_log = lambda **kwargs: iter(["abcd"])
    logs, truncated = observability_module._get_ray_task_logs_with_metadata(
        "ray-task-1",
        address=None,
        tail=10,
        max_bytes=4,
    )
    assert logs == {"out": "abcd", "err": "abcd"}
    assert truncated == {"out": False, "err": False}


def test_bounded_log_text_caps_redaction_expansion(settings) -> None:
    settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"token"]}
    text, truncated = observability_module._bounded_log_text(["token"], max_bytes=1)
    assert text == "["
    assert truncated is True

    text, truncated = observability_module._bounded_log_text(["abcde"], max_bytes=4)
    assert text == "[TRU"
    assert truncated is True


def test_isoformat_treats_naive_model_timestamp_as_utc() -> None:
    assert observability_module._isoformat(datetime(2026, 7, 19, 12)) == ("2026-07-19T12:00:00Z")


def test_observability_redacts_state_and_log_payloads(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265",
        "REDACT_PATTERNS": [r"access[_-]?token"],
    }
    fake_state = SimpleNamespace(
        get_task=lambda **kwargs: [
            SimpleNamespace(asdict=lambda: {"metadata": {"access_token": "secret-value"}})
        ],
        get_log=lambda **kwargs: iter(["access-token=secret-value\n"]),
    )
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    assert "secret-value" not in str(get_ray_task_state("ray-task-1"))
    assert "secret-value" not in str(get_ray_task_logs("ray-task-1"))


def test_get_ray_task_state_wraps_state_api_errors(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265",
    }
    fake_state = SimpleNamespace(
        get_task=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        get_log=lambda **kwargs: [],
    )
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    with pytest.raises(WorkflowObservabilityError, match="State API is unavailable"):
        get_ray_task_state("ray-task-1")


@pytest.mark.parametrize("progress_data", [None, ""])
def test_get_workflow_progress_returns_none_without_snapshot(progress_data) -> None:
    execution = SimpleNamespace(progress_data=progress_data, task_id="task-1")

    assert get_workflow_progress(execution) is None
    assert get_workflow_graph(execution) is None
    assert get_workflow_node(execution, "0.0") is None


@pytest.mark.parametrize("progress_data", ["{", "[]"])
def test_get_workflow_progress_rejects_invalid_snapshots(progress_data) -> None:
    execution = SimpleNamespace(progress_data=progress_data, task_id="task-1")

    with pytest.raises(WorkflowObservabilityError):
        get_workflow_progress(execution)


def test_get_workflow_progress_rejects_mismatched_versioned_run(db) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-mismatched-run",
        callable_path="tasks.workflow",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=4,
        workflow_run_id="00000000-0000-0000-0000-000000000034",
    )
    execution.progress_data = json.dumps(
        {
            "schema_version": 2,
            "run_identity": {
                "schema_version": 1,
                "run_id": "00000000-0000-0000-0000-000000000035",
                "task_execution_pk": execution.pk,
                "attempt_number": 2,
                "execution_generation": 4,
            },
            "revision": 1,
        }
    )
    execution.save(update_fields=["progress_data"])

    with pytest.raises(WorkflowObservabilityError, match="belongs to another run"):
        get_workflow_progress(execution)


@pytest.mark.parametrize("schema_version", [True, "2"])
def test_get_workflow_progress_rejects_invalid_schema_version(schema_version) -> None:
    execution = SimpleNamespace(
        progress_data=json.dumps({"schema_version": schema_version}),
        task_id="workflow-invalid-version",
    )

    with pytest.raises(WorkflowObservabilityError, match="invalid schema version"):
        get_workflow_progress(execution)


def test_get_workflow_progress_requires_identity_for_versioned_snapshot() -> None:
    execution = SimpleNamespace(
        progress_data=json.dumps({"schema_version": 2, "revision": 1}),
        task_id="workflow-missing-run-identity",
    )

    with pytest.raises(WorkflowObservabilityError, match="must contain a run identity"):
        get_workflow_progress(execution)


def test_get_workflow_graph_builds_legacy_edges() -> None:
    execution = SimpleNamespace(
        task_id="task-1",
        progress_data=json.dumps(
            {
                "nodes": [
                    {"node_id": "0.0", "dependencies": []},
                    {"node_id": "0.1", "dependencies": ["0.0"]},
                    "ignored",
                ]
            }
        ),
    )

    assert get_workflow_graph(execution) == {
        "nodes": [
            {"node_id": "0.0", "dependencies": []},
            {"node_id": "0.1", "dependencies": ["0.0"]},
            "ignored",
        ],
        "edges": [{"source": "0.0", "target": "0.1"}],
    }

    execution.progress_data = json.dumps({"nodes": "not-a-list"})
    assert get_workflow_graph(execution) == {"nodes": [], "edges": []}

    execution.progress_data = json.dumps({"nodes": [{"node_id": "0.0", "dependencies": None}]})
    graph = get_workflow_graph(execution)
    assert graph is not None
    assert graph["edges"] == []


@pytest.mark.parametrize(
    "graph",
    [
        {"nodes": None, "edges": []},
        {"nodes": [], "edges": "invalid"},
    ],
)
def test_get_workflow_graph_rejects_malformed_nested_shapes(graph) -> None:
    execution = SimpleNamespace(
        task_id="task-malformed-graph",
        progress_data=json.dumps({"graph": graph}),
    )

    with pytest.raises(WorkflowObservabilityError, match="node and edge lists"):
        get_workflow_graph(execution)


def test_get_ray_task_state_handles_none_and_object_shapes(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {"RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265"}
    result = None

    def _get_task(**kwargs):
        return result

    fake_state = SimpleNamespace(get_task=_get_task, get_log=lambda **kwargs: [])
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    assert get_ray_task_state("ray-task-1") == []

    result = [
        {"state": "RUNNING"},
        SimpleNamespace(state="FAILED"),
        object(),
    ]
    attempts = get_ray_task_state("ray-task-1")

    assert attempts[0] == {"state": "RUNNING"}
    assert attempts[1] == {"state": "FAILED"}
    assert attempts[2] == {"record": "unsupported"}


def test_get_ray_task_state_does_not_render_unknown_objects(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {"RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265"}

    class HostileRecord:
        __slots__ = ()

        def __str__(self) -> str:
            raise AssertionError("unknown records must not be rendered")

    fake_state = SimpleNamespace(
        get_task=lambda **kwargs: HostileRecord(),
        get_log=lambda **kwargs: [],
    )
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    assert get_ray_task_state("ray-task-1") == [{"record": "unsupported"}]


@pytest.mark.parametrize("tail", [0, 1001])
def test_get_ray_task_logs_validates_tail(tail) -> None:
    with pytest.raises(ValueError, match="between 1 and 1000"):
        get_ray_task_logs("ray-task-1", tail=tail)


@pytest.mark.parametrize("max_bytes", [0, MAX_RAY_LOG_MAX_BYTES + 1])
def test_get_ray_task_logs_validates_byte_limit(max_bytes) -> None:
    with pytest.raises(ValueError, match="max_bytes must be between"):
        get_ray_task_logs("ray-task-1", max_bytes=max_bytes)


def test_get_ray_task_logs_wraps_state_api_errors(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {"RAY_STATE_API_ADDRESS": "http://ray-dashboard:8265"}
    fake_state = SimpleNamespace(
        get_task=lambda **kwargs: None,
        get_log=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("logs offline")),
    )
    monkeypatch.setitem(sys.modules, "ray.util.state", fake_state)

    with pytest.raises(WorkflowObservabilityError, match="Log API is unavailable"):
        get_ray_task_logs("ray-task-1")


def test_state_api_address_uses_explicit_or_initialized_ray(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {}

    assert observability_module._state_api_address("http://explicit:8265") == (
        "http://explicit:8265"
    )

    monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
    assert observability_module._state_api_address(None) is None


def test_state_api_address_is_required_without_ray(monkeypatch, settings) -> None:
    settings.DJANGO_RAY = {}
    monkeypatch.setitem(sys.modules, "ray", None)

    with pytest.raises(ImproperlyConfigured, match="RAY_STATE_API_ADDRESS is required"):
        observability_module._state_api_address(None)


def test_workflow_node_snapshot_is_durable_first(workflow_execution) -> None:
    snapshot = get_workflow_node_snapshot(workflow_execution, "0.0")

    assert snapshot is not None
    assert snapshot["schema"] == "django-ray.workflow-node-snapshot"
    assert snapshot["attempt_number"] == 2
    assert snapshot["execution_generation"] == 4
    assert snapshot["workflow_run_id"] == "00000000-0000-0000-0000-000000000031"
    assert snapshot["workflow_revision"] == 3
    assert snapshot["node"]["node_id"] == "0.0"
    assert snapshot["live"] == {
        "status": "not_requested",
        "reason": None,
        "ray_state": None,
        "logs": None,
        "logs_truncated": None,
    }
    assert get_workflow_node_snapshot(workflow_execution, "missing") is None


def test_workflow_node_snapshot_handles_invalid_execution_metadata(db) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-invalid-execution",
        callable_path="tasks.workflow",
        progress_data=json.dumps(
            {
                "graph": {
                    "nodes": [{"node_id": "0.0", "execution": "invalid"}],
                    "edges": [],
                }
            }
        ),
    )

    snapshot = get_workflow_node_snapshot(execution, "0.0", include_live=True)

    assert snapshot is not None
    assert snapshot["live"]["status"] == "unavailable"
    assert snapshot["live"]["reason"] == "ray_task_id_unavailable"


def test_workflow_node_snapshot_reports_missing_ray_id(workflow_execution) -> None:
    snapshot = get_workflow_node_snapshot(workflow_execution, "0.1", include_live=True)

    assert snapshot is not None
    assert snapshot["live"]["status"] == "unavailable"
    assert snapshot["live"]["reason"] == "ray_task_id_unavailable"


def test_workflow_node_snapshot_degrades_when_ray_state_is_unavailable(
    workflow_execution,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        observability_module,
        "get_ray_task_state",
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("private topology")),
    )

    snapshot = get_workflow_node_snapshot(workflow_execution, "0.0", include_live=True)

    assert snapshot is not None
    assert snapshot["node"]["node_id"] == "0.0"
    assert snapshot["live"]["status"] == "unavailable"
    assert snapshot["live"]["reason"] == "state_api_unavailable"
    assert "private topology" not in str(snapshot)


def test_workflow_node_snapshot_adds_bounded_live_logs(
    workflow_execution,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        observability_module,
        "get_ray_task_state",
        lambda _task_id: [{"state": "RUNNING"}],
    )
    monkeypatch.setattr(
        observability_module,
        "_get_ray_task_logs_with_metadata",
        lambda *_args, **_kwargs: (
            {"out": "line", "err": ""},
            {"out": True, "err": False},
        ),
    )

    snapshot = get_workflow_node_snapshot(
        workflow_execution,
        "0.0",
        include_logs=True,
        tail=20,
        max_log_bytes=100,
    )

    assert snapshot is not None
    assert snapshot["live"] == {
        "status": "available",
        "reason": None,
        "ray_state": [{"state": "RUNNING"}],
        "logs": {"out": "line", "err": ""},
        "logs_truncated": {"out": True, "err": False},
    }


def test_workflow_node_snapshot_reports_log_api_failure(
    workflow_execution,
    monkeypatch,
) -> None:
    monkeypatch.setattr(observability_module, "get_ray_task_state", lambda _task_id: [])
    monkeypatch.setattr(
        observability_module,
        "_get_ray_task_logs_with_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("secret host")),
    )

    snapshot = get_workflow_node_snapshot(workflow_execution, "0.0", include_logs=True)

    assert snapshot is not None
    assert snapshot["live"]["status"] == "partial"
    assert snapshot["live"]["reason"] == "log_api_unavailable"
    assert "secret host" not in str(snapshot)


def test_workflow_node_snapshot_validates_log_bounds(workflow_execution) -> None:
    with pytest.raises(ValueError, match="tail must be between"):
        get_workflow_node_snapshot(workflow_execution, "0.0", include_logs=True, tail=0)
