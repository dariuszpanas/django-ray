"""Tests for module-level Ray task and workflow executors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

import django_ray.runtime.remote as remote_module
from django_ray.execution_codec import (
    ExecutionIdentity,
    ExecutionRequestRejection,
    NestedExecutionRequestRejected,
    NestedExecutionRequestRejection,
    decode_execution_completion,
    encode_execution_request,
)
from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.runtime.remote import (
    execute_django_task_remote,
    execute_workflow_step_remote,
)
from django_ray.workflow.progress.protocol import (
    WorkflowProgressEventKind,
    decode_workflow_progress_event,
)
from tests.unit._remote_helpers import (
    _OCCURRED_AT as _OCCURRED_AT,
)
from tests.unit._remote_helpers import (
    _STRICT_IDENTITY as _STRICT_IDENTITY,
)
from tests.unit._remote_helpers import (
    _WORKFLOW_PLAN as _WORKFLOW_PLAN,
)
from tests.unit._remote_helpers import (
    _WORKFLOW_RUN_IDENTITY as _WORKFLOW_RUN_IDENTITY,
)
from tests.unit._remote_helpers import (
    _execute_bound_workflow_step as _execute_bound_workflow_step,
)
from tests.unit._remote_helpers import (
    _execute_strict_request as _execute_strict_request,
)
from tests.unit._remote_helpers import (
    _producer_report as _producer_report,
)
from tests.unit._remote_helpers import (
    _progress_actor as _progress_actor,
)
from tests.unit._remote_helpers import (
    _progress_wire as _progress_wire,
)
from tests.unit._remote_helpers import (
    _ProgressActor as _ProgressActor,
)
from tests.unit._remote_helpers import (
    _RemoteMethod as _RemoteMethod,
)
from tests.unit._remote_helpers import (
    _strict_execution_request as _strict_execution_request,
)
from tests.unit._remote_helpers import (
    _strict_nested_workflow_kwargs as _strict_nested_workflow_kwargs,
)
from tests.unit._remote_helpers import (
    _strict_nested_workflow_request as _strict_nested_workflow_request,
)


def workflow_target(value: int, increment: int = 0) -> int:
    return value + increment


def preview_workflow_target(value: int) -> dict[str, object]:
    return {"result": value, "status": "ready"}


def failing_workflow_preview(_value: object) -> object:
    raise RuntimeError("preview failed")


def test_task_bootstrap_unpickles_before_django_ray_is_importable() -> None:
    import ray.cloudpickle as cloudpickle

    cloudpickle.register_pickle_by_value(remote_module)
    try:
        payload = cloudpickle.dumps(execute_django_task_remote)
    finally:
        cloudpickle.unregister_pickle_by_value(remote_module)

    unpickle_without_django_ray = """
import importlib.abc
import sys

import ray.cloudpickle as cloudpickle


class BlockDjangoRay(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "django_ray" or fullname.startswith("django_ray."):
            raise ModuleNotFoundError(fullname, name=fullname)
        return None


sys.meta_path.insert(0, BlockDjangoRay())
cloudpickle.loads(sys.stdin.buffer.read())
"""
    result = subprocess.run(
        [sys.executable, "-c", unpickle_without_django_ray],
        input=payload,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def failing_workflow_target(value: int) -> int:
    raise RuntimeError(f"failed:{value}")


def workflow_context_target() -> dict[str, object] | None:
    from django_ray.runtime.context import get_current_workflow_run_identity

    return get_current_workflow_run_identity()


def strict_workflow_context_target() -> dict[str, object]:
    from django_ray.runtime.context import get_current_task_context

    context = get_current_task_context()
    assert context is not None
    return {
        "task_pk": context.task_pk,
        "task_id": context.task_id,
        "attempt_number": context.attempt_number,
        "execution_generation": context.execution_generation,
        "execution_protocol_version": context.execution_protocol_version,
        "strict_execution_request": context.strict_execution_request,
        "compiled_graph_submission_transport": (context.compiled_graph_submission_transport),
        "runtime_env_hash": context.runtime_env_hash,
        "runtime_env_plan_identity": context.runtime_env_plan_identity,
    }


def strict_workflow_full_context_target(expected: dict[str, object]) -> str:
    """Assert the complete strict context inside a real Ray workflow leaf."""
    from dataclasses import asdict

    from django_ray.runtime.context import get_current_task_context

    context = get_current_task_context()
    assert context is not None
    assert asdict(context) == expected
    return "strict-context-ready"


def workflow_progress_target(value: int, *, fail: bool = False) -> int:
    from django_ray.workflows import report_progress

    assert report_progress(1, 2)
    assert report_progress(2, 2)
    if fail:
        raise RuntimeError(f"progress-failed:{value}")
    return value


def test_execute_django_task_remote_logs_failure(monkeypatch, capsys) -> None:
    payload = json.dumps({"success": False, "error": "boom"})
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.execute_task", lambda *args, **kwargs: payload
    )

    result = _execute_strict_request(encode_execution_request(_strict_execution_request()))

    captured = capsys.readouterr()
    assert result == payload
    assert "[Task 217] Starting: tests.strict_task" in captured.out
    assert "[Task 217] FAILED: boom" in captured.err


def test_execute_django_task_remote_logs_fixed_success_marker(monkeypatch, capsys) -> None:
    payload = json.dumps({"success": True, "result": {"password": "secret-value"}})
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.execute_task", lambda *args, **kwargs: payload
    )

    _execute_strict_request(encode_execution_request(_strict_execution_request()))

    captured = capsys.readouterr()
    assert "[Task 217] SUCCESS" in captured.out
    assert "secret-value" not in captured.out
    assert "result_type" not in captured.out
    assert "result_size_bytes" not in captured.out


def test_execute_django_task_remote_forwards_input_reference(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_execute(*args, **kwargs) -> str:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return json.dumps({"success": True, "result": None})

    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", fake_execute)

    request = replace(
        _strict_execution_request(),
        transport_version=2,
        serialized_args="null",
        serialized_kwargs="null",
        input_reference="resultfs://input",
    )
    _execute_strict_request(encode_execution_request(request))
    forwarded = captured["kwargs"]
    assert isinstance(forwarded, dict)
    assert forwarded["input_reference"] == "resultfs://input"


def test_execute_django_task_remote_propagates_attempt_and_generation(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_execute(*_args, **_kwargs) -> str:
        from django_ray.runtime.context import get_current_task_context

        context = get_current_task_context()
        assert context is not None
        captured.update(
            task_pk=context.task_pk,
            execution_protocol_version=context.execution_protocol_version,
            attempt_number=context.attempt_number,
            execution_generation=context.execution_generation,
            compiled_graph_submission_transport=(context.compiled_graph_submission_transport),
        )
        return json.dumps({"success": True, "result": None})

    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", fake_execute)

    _execute_strict_request(encode_execution_request(_strict_execution_request()))
    assert captured == {
        "task_pk": 217,
        "execution_protocol_version": 1,
        "attempt_number": 3,
        "execution_generation": 6,
        "compiled_graph_submission_transport": "direct-ray-core",
    }


def test_execute_django_task_remote_executes_strict_request_with_bound_context(
    monkeypatch,
    capsys,
) -> None:
    from django_ray.runtime.context import get_current_task_context

    observed: dict[str, object] = {}
    monkeypatch.setattr("django_ray.runtime.entrypoint.bootstrap_django", lambda: None)
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.load_task_input",
        lambda **values: (
            observed.update(input=values) or ["value"],
            {"increment": 2},
        ),
    )

    def strict_task(value: str, *, increment: int) -> dict[str, object]:
        context = get_current_task_context()
        assert context is not None
        observed["context"] = {
            "task_pk": context.task_pk,
            "task_id": context.task_id,
            "execution_protocol_version": context.execution_protocol_version,
            "attempt_number": context.attempt_number,
            "execution_generation": context.execution_generation,
            "runtime_env_profile": context.runtime_env_profile,
            "runtime_env_hash": context.runtime_env_hash,
            "runtime_env_plan_identity": context.runtime_env_plan_identity,
            "compiled_graph_submission_transport": (context.compiled_graph_submission_transport),
            "strict_execution_request": context.strict_execution_request,
        }
        return {"increment": increment, "value": value}

    monkeypatch.setattr(
        "django_ray.runtime.import_utils.import_callable",
        lambda path: observed.update(callable_path=path) or strict_task,
    )

    result = _execute_strict_request(encode_execution_request(_strict_execution_request()))
    decoded = decode_execution_completion(
        result,
        expected_identity=_STRICT_IDENTITY,
        expected_execution_protocol_version=1,
    ).completion

    assert decoded.success is True
    assert decoded.result == {"increment": 2, "value": "value"}
    assert decoded.identity == _STRICT_IDENTITY
    assert decoded.execution_protocol_version == 1
    assert decoded.executor_django_ray_version
    assert observed == {
        "input": {
            "args_json": '["value"]',
            "kwargs_json": '{"increment":2}',
            "input_reference": None,
        },
        "callable_path": "tests.strict_task",
        "context": {
            "task_pk": 217,
            "task_id": "00000000-0000-4000-8000-000000000217",
            "execution_protocol_version": 1,
            "attempt_number": 3,
            "execution_generation": 6,
            "runtime_env_profile": "thin",
            "runtime_env_hash": "a" * 64,
            "runtime_env_plan_identity": {"profile": "thin", "schema_version": 1},
            "compiled_graph_submission_transport": "direct-ray-core",
            "strict_execution_request": True,
        },
    }
    assert get_current_task_context() is None
    captured = capsys.readouterr()
    assert "[Task 217] Starting: tests.strict_task" in captured.out
    assert "[Task 217] SUCCESS" in captured.out


def test_execute_django_task_remote_returns_enriched_strict_failure(
    monkeypatch,
    capsys,
) -> None:
    secret = "password=application-secret"
    monkeypatch.setattr("django_ray.runtime.entrypoint.bootstrap_django", lambda: None)
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.load_task_input",
        lambda **_values: ([], {}),
    )

    def fail() -> None:
        raise ValueError(secret)

    monkeypatch.setattr("django_ray.runtime.import_utils.import_callable", lambda _path: fail)

    result = _execute_strict_request(encode_execution_request(_strict_execution_request()))
    decoded = decode_execution_completion(
        result,
        expected_identity=_STRICT_IDENTITY,
        expected_execution_protocol_version=1,
    ).completion

    assert decoded.success is False
    assert decoded.result is None
    assert decoded.error == secret
    assert decoded.exception_type == "builtins.ValueError"
    assert decoded.retryable is True
    captured = capsys.readouterr()
    assert secret not in captured.err
    assert "[Task 217] FAILED: [REDACTED]" in captured.err


@pytest.mark.parametrize(
    ("serialized", "identity", "expected_protocol", "classification"),
    [
        (
            encode_execution_request(_strict_execution_request()),
            replace(_STRICT_IDENTITY, task_id="00000000-0000-4000-8000-000000000999"),
            1,
            ExecutionRequestRejection.IDENTITY_MISMATCH,
        ),
        (
            encode_execution_request(_strict_execution_request()),
            _STRICT_IDENTITY,
            2,
            ExecutionRequestRejection.PROTOCOL_MISMATCH,
        ),
        (
            '{"request_schema":"django-ray.execution-request","secret":"password=decoder-secret"}',
            _STRICT_IDENTITY,
            1,
            ExecutionRequestRejection.INVALID_VERSIONED,
        ),
        (
            json.dumps(
                {
                    **json.loads(encode_execution_request(_strict_execution_request())),
                    "execution_protocol_version": 2,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            _STRICT_IDENTITY,
            2,
            ExecutionRequestRejection.UNSUPPORTED_PROTOCOL,
        ),
    ],
)
def test_strict_request_rejection_precedes_all_application_side_effects(
    monkeypatch,
    serialized: str,
    identity: ExecutionIdentity,
    expected_protocol: int,
    classification: ExecutionRequestRejection,
) -> None:
    def unexpected(*_args, **_kwargs):
        pytest.fail("application execution started before strict request rejection")

    monkeypatch.setattr("django_ray.runtime.entrypoint.bootstrap_django", unexpected)
    monkeypatch.setattr("django_ray.runtime.entrypoint.load_task_input", unexpected)
    monkeypatch.setattr("django_ray.runtime.import_utils.import_callable", unexpected)
    monkeypatch.setattr("django_ray.runtime.entrypoint._invoke_task_callable", unexpected)
    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", unexpected)

    result = _execute_strict_request(
        serialized,
        identity=identity,
        expected_protocol=expected_protocol,
    )
    decoded = decode_execution_completion(
        result,
        expected_identity=identity,
        expected_execution_protocol_version=expected_protocol,
        supported_protocols=ExecutionProtocolRange(1, 2),
    ).completion

    assert decoded.success is False
    assert decoded.result is None
    assert decoded.result_reference is None
    assert decoded.traceback is None
    assert decoded.exception_type == "RayExecutionRequestIncompatible"
    assert decoded.retryable is False
    assert decoded.error == f"execution request rejected: {classification.value}"
    assert "decoder-secret" not in result


def test_execute_workflow_step_bootstraps_and_reports_completion(monkeypatch) -> None:
    bootstrapped: list[bool] = []
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.bootstrap_django",
        lambda: bootstrapped.append(True),
    )
    monkeypatch.setattr(remote_module, "_ray_execution_metadata", lambda: {"ray_task_id": "1"})
    actor = _ProgressActor()
    log_context: dict[str, object] = {}
    monkeypatch.setattr(
        "django_ray.logging.get_logger",
        lambda _name, **kwargs: (
            log_context.update(kwargs)
            or SimpleNamespace(info=lambda *_args: None, exception=lambda *_args: None)
        ),
    )
    run_identity = dict(_WORKFLOW_RUN_IDENTITY)

    result = _execute_bound_workflow_step(
        "tests.unit.test_remote.workflow_target",
        True,
        (),
        {"increment": 2},
        {},
        9,
        actor,
        "0.0",
        3,
        workflow_run_identity=run_identity,
    )

    assert result == 5
    assert bootstrapped == [True]
    assert len(actor.ingest.calls) == 2
    events = [
        decode_workflow_progress_event(
            call[0],
            expected_run_identity=run_identity,
        )
        for call in actor.ingest.calls
    ]
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.STARTED,
        WorkflowProgressEventKind.COMPLETED,
    ]
    assert events[0].payload == {
        "execution": {
            "assigned_resources": {},
            "ray_job_id": None,
            "ray_node_id": None,
            "ray_task_id": "1",
            "ray_worker_id": None,
        },
        "label": "workflow_target",
        "node_id": "0.0",
    }
    assert events[1].payload == {
        "label": "workflow_target",
        "node_id": "0.0",
    }
    assert all(event.run_identity == run_identity for event in events)
    assert log_context["workflow_run_id"] == run_identity["run_id"]
    assert log_context["workflow_attempt_number"] == 2
    assert log_context["workflow_execution_generation"] == 6


def test_execute_workflow_step_exposes_run_identity_to_leaf(monkeypatch) -> None:
    from django_ray.runtime.context import get_current_workflow_run_identity

    monkeypatch.setattr(remote_module, "_ray_execution_metadata", dict)
    run_identity = {
        "schema_version": 1,
        "run_id": "00000000-0000-0000-0000-000000000012",
        "task_execution_pk": 10,
        "attempt_number": 4,
        "execution_generation": 8,
    }

    result = _execute_bound_workflow_step(
        "tests.unit.test_remote.workflow_context_target",
        False,
        (),
        {},
        {},
        10,
        None,
        "0.0",
        workflow_run_identity=run_identity,
    )

    assert result == run_identity
    assert get_current_workflow_run_identity() is None


def test_strict_workflow_step_installs_exact_nested_context() -> None:
    callable_path = f"{__name__}.strict_workflow_context_target"
    workflow_run_id = "00000000-0000-4000-8000-000000000511"
    node_id = "0.strict"
    serialized, runtime_identity = _strict_nested_workflow_request(
        callable_path=callable_path,
        workflow_run_id=workflow_run_id,
        node_id=node_id,
    )
    run_identity = {
        "schema_version": 1,
        "run_id": workflow_run_id,
        "task_execution_pk": _STRICT_IDENTITY.task_execution_pk,
        "attempt_number": _STRICT_IDENTITY.attempt_number,
        "execution_generation": _STRICT_IDENTITY.execution_generation,
    }

    result = execute_workflow_step_remote(
        callable_path,
        False,
        (),
        {},
        {},
        _STRICT_IDENTITY.task_execution_pk,
        None,
        node_id,
        workflow_run_identity=run_identity,
        **_strict_nested_workflow_kwargs(
            serialized,
            runtime_identity,
            workflow_run_id=workflow_run_id,
            node_id=node_id,
        ),
    )

    assert result == {
        "task_pk": _STRICT_IDENTITY.task_execution_pk,
        "task_id": _STRICT_IDENTITY.task_id,
        "attempt_number": _STRICT_IDENTITY.attempt_number,
        "execution_generation": _STRICT_IDENTITY.execution_generation,
        "execution_protocol_version": 1,
        "strict_execution_request": True,
        "compiled_graph_submission_transport": "direct-ray-core",
        "runtime_env_hash": "",
        "runtime_env_plan_identity": runtime_identity,
    }


def test_strict_workflow_step_rejects_mixed_progress_identity_before_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callable_path = f"{__name__}.strict_workflow_context_target"
    workflow_run_id = "00000000-0000-4000-8000-000000000512"
    node_id = "0.strict"
    serialized, runtime_identity = _strict_nested_workflow_request(
        callable_path=callable_path,
        workflow_run_id=workflow_run_id,
        node_id=node_id,
    )
    bootstrapped: list[bool] = []
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.bootstrap_django",
        lambda: bootstrapped.append(True),
    )

    with pytest.raises(NestedExecutionRequestRejected) as captured:
        execute_workflow_step_remote(
            callable_path,
            True,
            (),
            {},
            {},
            _STRICT_IDENTITY.task_execution_pk,
            None,
            node_id,
            workflow_run_identity={
                "schema_version": 1,
                "run_id": "mixed-secret-run",
                "task_execution_pk": _STRICT_IDENTITY.task_execution_pk,
                "attempt_number": _STRICT_IDENTITY.attempt_number,
                "execution_generation": _STRICT_IDENTITY.execution_generation,
            },
            **_strict_nested_workflow_kwargs(
                serialized,
                runtime_identity,
                workflow_run_id=workflow_run_id,
                node_id=node_id,
            ),
        )

    assert captured.value.classification is NestedExecutionRequestRejection.BOUNDARY_MISMATCH
    assert str(captured.value) == "nested execution request rejected: boundary_mismatch"
    assert "mixed-secret-run" not in str(captured.value)
    assert bootstrapped == []


def test_strict_workflow_step_rejects_mixed_positional_node_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callable_path = f"{__name__}.strict_workflow_context_target"
    workflow_run_id = "00000000-0000-4000-8000-000000000513"
    node_id = "0.strict"
    serialized, runtime_identity = _strict_nested_workflow_request(
        callable_path=callable_path,
        workflow_run_id=workflow_run_id,
        node_id=node_id,
    )
    monkeypatch.setattr(
        "django_ray.runtime.import_utils.import_callable",
        lambda _path: pytest.fail("callable imported before node binding rejection"),
    )

    with pytest.raises(NestedExecutionRequestRejected) as captured:
        execute_workflow_step_remote(
            callable_path,
            False,
            (),
            {},
            {},
            _STRICT_IDENTITY.task_execution_pk,
            None,
            "mixed-secret-node",
            **_strict_nested_workflow_kwargs(
                serialized,
                runtime_identity,
                workflow_run_id=workflow_run_id,
                node_id=node_id,
            ),
        )

    assert captured.value.classification is NestedExecutionRequestRejection.BOUNDARY_MISMATCH
    assert "mixed-secret-node" not in str(captured.value)


def test_strict_workflow_step_rejects_mixed_output_preview_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callable_path = f"{__name__}.strict_workflow_context_target"
    output_preview_path = f"{__name__}.preview_workflow_target"
    workflow_run_id = "00000000-0000-4000-8000-000000000515"
    node_id = "0.strict"
    serialized, runtime_identity = _strict_nested_workflow_request(
        callable_path=callable_path,
        workflow_run_id=workflow_run_id,
        node_id=node_id,
        output_preview_path=output_preview_path,
    )
    monkeypatch.setattr(
        "django_ray.runtime.import_utils.import_callable",
        lambda _path: pytest.fail("callable imported before preview binding rejection"),
    )

    with pytest.raises(NestedExecutionRequestRejected) as captured:
        execute_workflow_step_remote(
            callable_path,
            False,
            (),
            {},
            {},
            _STRICT_IDENTITY.task_execution_pk,
            None,
            node_id,
            output_preview_path="mixed-secret-preview",
            **_strict_nested_workflow_kwargs(
                serialized,
                runtime_identity,
                workflow_run_id=workflow_run_id,
                node_id=node_id,
            ),
        )

    assert captured.value.classification is NestedExecutionRequestRejection.CALLABLE_MISMATCH
    assert str(captured.value) == "nested execution request rejected: callable_mismatch"
    assert "mixed-secret-preview" not in str(captured.value)


def test_execute_workflow_step_reports_failure() -> None:
    import ray

    assert not ray.is_initialized()
    actor = _ProgressActor()
    run_identity = dict(_WORKFLOW_RUN_IDENTITY)

    with pytest.raises(RuntimeError, match="failed:3"):
        _execute_bound_workflow_step(
            "tests.unit.test_remote.failing_workflow_target",
            False,
            (),
            {},
            {},
            run_identity["task_execution_pk"],
            actor,
            "0.1",
            3,
            workflow_run_identity=run_identity,
        )

    events = [
        decode_workflow_progress_event(
            call[0],
            expected_run_identity=run_identity,
        )
        for call in actor.ingest.calls
    ]
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.STARTED,
        WorkflowProgressEventKind.FAILED,
    ]
    assert events[-1].payload == {
        "error": "failed:3",
        "label": "failing_workflow_target",
        "node_id": "0.1",
    }
    assert not ray.is_initialized()


def test_workflow_step_sanitizes_nested_rejection_before_progress_and_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ray.exceptions import RayTaskError

    cause = NestedExecutionRequestRejected(NestedExecutionRequestRejection.IDENTITY_MISMATCH)
    wrapped = RayTaskError(
        "nested_leaf",
        "secret-bearing nested traceback",
        cause,
        proctitle="ray::nested_leaf",
        pid=123,
        ip="127.0.0.1",
    )

    def reject() -> None:
        raise wrapped

    safe_logs: list[str] = []
    monkeypatch.setattr(
        "django_ray.runtime.import_utils.import_callable",
        lambda _path: reject,
    )
    monkeypatch.setattr("django_ray.logging.configure_default_logging", lambda: None)
    monkeypatch.setattr(
        "django_ray.logging.get_logger",
        lambda *_args, **_kwargs: SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            error=lambda message, **_kwargs: safe_logs.append(message),
            exception=lambda *_args, **_kwargs: pytest.fail(
                "nested rejection emitted raw exception logging"
            ),
        ),
    )
    monkeypatch.setattr(remote_module, "_ray_execution_metadata", dict)
    actor = _ProgressActor()
    run_identity = dict(_WORKFLOW_RUN_IDENTITY)

    with pytest.raises(NestedExecutionRequestRejected) as caught:
        _execute_bound_workflow_step(
            "tests.unit.test_remote.nested_rejection_target",
            False,
            (),
            {},
            {},
            run_identity["task_execution_pk"],
            actor,
            "0.nested",
            workflow_run_identity=run_identity,
        )

    assert caught.value is cause
    events = [
        decode_workflow_progress_event(
            call[0],
            expected_run_identity=run_identity,
        )
        for call in actor.ingest.calls
    ]
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.STARTED,
        WorkflowProgressEventKind.FAILED,
    ]
    assert events[-1].payload["error"] == ("nested execution request rejected: identity_mismatch")
    assert safe_logs == ["Workflow step rejected nested execution request"]
    assert "secret-bearing" not in b"".join(call[0] for call in actor.ingest.calls).decode()


def test_execute_workflow_step_ignores_progress_reporting_failures() -> None:
    actor = _ProgressActor()

    def reject_wire(_wire: bytes) -> None:
        raise RuntimeError("progress actor unavailable")

    actor.ingest.remote = reject_wire

    result = _execute_bound_workflow_step(
        "tests.unit.test_remote.workflow_target",
        False,
        (),
        {"increment": 2},
        {},
        9,
        actor,
        "0.0",
        3,
        workflow_run_identity=dict(_WORKFLOW_RUN_IDENTITY),
    )

    assert result == 5


def test_execute_workflow_step_reports_explicit_output_preview(monkeypatch) -> None:
    actor = _ProgressActor()
    run_identity = dict(_WORKFLOW_RUN_IDENTITY)
    monkeypatch.setattr(remote_module, "_ray_execution_metadata", dict)

    result = _execute_bound_workflow_step(
        "tests.unit.test_remote.workflow_target",
        False,
        (),
        {"increment": 2},
        {},
        9,
        actor,
        "0.0",
        3,
        output_preview_path="tests.unit.test_remote.preview_workflow_target",
        workflow_run_identity=run_identity,
    )

    assert result == 5
    events = [
        decode_workflow_progress_event(
            call[0],
            expected_run_identity=run_identity,
        )
        for call in actor.ingest.calls
    ]
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.STARTED,
        WorkflowProgressEventKind.OUTPUT_PREVIEW,
        WorkflowProgressEventKind.COMPLETED,
    ]
    assert events[1].payload == {
        "node_id": "0.0",
        "output_preview": {
            "schema_version": 1,
            "availability": "AVAILABLE",
            "value": {"result": 5, "status": "ready"},
        },
    }


def test_output_preview_failure_cannot_replace_workflow_success(monkeypatch) -> None:
    actor = _ProgressActor()
    run_identity = dict(_WORKFLOW_RUN_IDENTITY)
    monkeypatch.setattr(remote_module, "_ray_execution_metadata", dict)

    class _Logger:
        def info(self, *_args: object, **_kwargs: object) -> None:
            pass

        def exception(self, *_args: object, **_kwargs: object) -> None:
            pass

        def warning(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("diagnostic logging failed")

    monkeypatch.setattr("django_ray.logging.get_logger", lambda *_args, **_kwargs: _Logger())

    result = _execute_bound_workflow_step(
        "tests.unit.test_remote.workflow_target",
        False,
        (),
        {},
        {},
        9,
        actor,
        "0.0",
        3,
        output_preview_path="tests.unit.test_remote.failing_workflow_preview",
        workflow_run_identity=run_identity,
    )

    assert result == 3
    events = [
        decode_workflow_progress_event(
            call[0],
            expected_run_identity=run_identity,
        )
        for call in actor.ingest.calls
    ]
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.STARTED,
        WorkflowProgressEventKind.OUTPUT_PREVIEW,
        WorkflowProgressEventKind.COMPLETED,
    ]
    assert events[1].payload["output_preview"] == {
        "schema_version": 1,
        "availability": "FAILED",
        "value": None,
    }


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_output_preview_import_obeys_exception_boundary(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    actor = _ProgressActor()
    run_identity = dict(_WORKFLOW_RUN_IDENTITY)
    monkeypatch.setattr(remote_module, "_ray_execution_metadata", dict)
    from django_ray.runtime import import_utils

    original_import = import_utils.import_callable

    def import_with_preview_failure(path: str):
        if path == "tests.unit.test_remote.preview-workflow-import-failure":
            raise failure_type("preview import boundary")
        return original_import(path)

    monkeypatch.setattr(import_utils, "import_callable", import_with_preview_failure)

    if failure_type is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt, match="preview import boundary"):
            _execute_bound_workflow_step(
                "tests.unit.test_remote.workflow_target",
                False,
                (),
                {},
                {},
                9,
                actor,
                "0.0",
                3,
                output_preview_path=("tests.unit.test_remote.preview-workflow-import-failure"),
                workflow_run_identity=run_identity,
            )
        return

    assert (
        _execute_bound_workflow_step(
            "tests.unit.test_remote.workflow_target",
            False,
            (),
            {},
            {},
            9,
            actor,
            "0.0",
            3,
            output_preview_path="tests.unit.test_remote.preview-workflow-import-failure",
            workflow_run_identity=run_identity,
        )
        == 3
    )
    events = [
        decode_workflow_progress_event(call[0], expected_run_identity=run_identity)
        for call in actor.ingest.calls
    ]
    assert events[1].payload["output_preview"] == {
        "schema_version": 1,
        "availability": "FAILED",
        "value": None,
    }


def test_output_preview_logging_interrupt_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _ProgressActor()
    monkeypatch.setattr(remote_module, "_ray_execution_metadata", dict)

    class _Logger:
        def info(self, *_args: object, **_kwargs: object) -> None:
            pass

        def exception(self, *_args: object, **_kwargs: object) -> None:
            pass

        def warning(self, *_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt("diagnostic logging cancelled")

    monkeypatch.setattr("django_ray.logging.get_logger", lambda *_args, **_kwargs: _Logger())

    with pytest.raises(KeyboardInterrupt, match="diagnostic logging cancelled"):
        _execute_bound_workflow_step(
            "tests.unit.test_remote.workflow_target",
            False,
            (),
            {},
            {},
            9,
            actor,
            "0.0",
            3,
            output_preview_path="tests.unit.test_remote.failing_workflow_preview",
            workflow_run_identity=dict(_WORKFLOW_RUN_IDENTITY),
        )


@pytest.mark.parametrize(
    ("fail", "terminal_kind"),
    [
        (False, WorkflowProgressEventKind.COMPLETED),
        (True, WorkflowProgressEventKind.FAILED),
    ],
)
def test_execute_workflow_step_orders_terminal_latest_value_before_terminal_event(
    monkeypatch,
    fail: bool,
    terminal_kind: WorkflowProgressEventKind,
) -> None:
    actor = _ProgressActor()

    def pending_remote(*args) -> object:
        actor.ingest.calls.append(args)
        return object()

    actor.ingest.remote = pending_remote
    monkeypatch.setattr(
        "django_ray.workflow.progress.producer._poll_ray_ack",
        lambda _reference: "pending",
    )
    run_identity = dict(_WORKFLOW_RUN_IDENTITY)

    if fail:
        with pytest.raises(RuntimeError, match="progress-failed:3"):
            _execute_bound_workflow_step(
                "tests.unit.test_remote.workflow_progress_target",
                False,
                (),
                {"fail": True},
                {},
                9,
                actor,
                "0.0",
                3,
                workflow_run_identity=run_identity,
            )
    else:
        assert (
            _execute_bound_workflow_step(
                "tests.unit.test_remote.workflow_progress_target",
                False,
                (),
                {},
                {},
                9,
                actor,
                "0.0",
                3,
                workflow_run_identity=run_identity,
            )
            == 3
        )

    events = [
        decode_workflow_progress_event(
            call[0],
            expected_run_identity=run_identity,
        )
        for call in actor.ingest.calls
    ]
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.STARTED,
        WorkflowProgressEventKind.APPLICATION_PROGRESS,
        WorkflowProgressEventKind.APPLICATION_PROGRESS,
        WorkflowProgressEventKind.PRODUCER_REPORT,
        terminal_kind,
    ]
    assert events[1].payload["current"] == 1.0
    assert events[2].payload["current"] == 2.0
    assert events[3].payload["terminal_handoff"] == "submitted"


@pytest.mark.parametrize("fail", [False, True])
def test_execute_workflow_step_preserves_outcome_when_producer_report_fails(
    fail: bool,
) -> None:
    actor = _ProgressActor()
    attempted_kinds: list[WorkflowProgressEventKind] = []
    run_identity = dict(_WORKFLOW_RUN_IDENTITY)

    def selective_failure(wire: bytes) -> None:
        event = decode_workflow_progress_event(
            wire,
            expected_run_identity=run_identity,
        )
        attempted_kinds.append(event.kind)
        if event.kind is WorkflowProgressEventKind.PRODUCER_REPORT:
            raise RuntimeError("producer report unavailable")

    actor.ingest.remote = selective_failure

    if fail:
        with pytest.raises(RuntimeError, match="progress-failed:3"):
            _execute_bound_workflow_step(
                "tests.unit.test_remote.workflow_progress_target",
                False,
                (),
                {"fail": True},
                {},
                9,
                actor,
                "0.0",
                3,
                workflow_run_identity=run_identity,
            )
        terminal_kind = WorkflowProgressEventKind.FAILED
    else:
        assert (
            _execute_bound_workflow_step(
                "tests.unit.test_remote.workflow_progress_target",
                False,
                (),
                {},
                {},
                9,
                actor,
                "0.0",
                3,
                workflow_run_identity=run_identity,
            )
            == 3
        )
        terminal_kind = WorkflowProgressEventKind.COMPLETED

    assert attempted_kinds == [
        WorkflowProgressEventKind.STARTED,
        WorkflowProgressEventKind.APPLICATION_PROGRESS,
        WorkflowProgressEventKind.APPLICATION_PROGRESS,
        WorkflowProgressEventKind.PRODUCER_REPORT,
        terminal_kind,
    ]


def test_ray_execution_metadata_handles_context_and_runtime_errors(monkeypatch) -> None:
    context = SimpleNamespace(
        get_task_id=lambda: "task",
        get_job_id=lambda: "job",
        get_node_id=lambda: "node",
        get_worker_id=lambda: "worker",
        get_assigned_resources=lambda: {"CPU": 1},
    )
    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            is_initialized=lambda: True,
            get_runtime_context=lambda: context,
        ),
    )

    assert remote_module._ray_execution_metadata() == {
        "ray_task_id": "task",
        "ray_job_id": "job",
        "ray_node_id": "node",
        "ray_worker_id": "worker",
        "assigned_resources": {"CPU": 1},
    }

    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            is_initialized=lambda: True,
            get_runtime_context=lambda: (_ for _ in ()).throw(RuntimeError("outside Ray")),
        ),
    )
    assert remote_module._ray_execution_metadata() == {}


def test_ray_execution_metadata_skips_context_when_ray_is_uninitialized(
    monkeypatch,
) -> None:
    context_lookups: list[bool] = []
    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            is_initialized=lambda: False,
            get_runtime_context=lambda: context_lookups.append(True),
        ),
    )

    assert remote_module._ray_execution_metadata() == {}
    assert context_lookups == []


def test_ray_execution_metadata_does_not_import_ray() -> None:
    probe = """
import json
import sys

ray_loaded_before = "ray" in sys.modules
from django_ray.runtime.remote import _ray_execution_metadata

payload = {
    "ray_loaded_before": ray_loaded_before,
    "ray_loaded_after_module_import": "ray" in sys.modules,
    "metadata": _ray_execution_metadata(),
    "ray_loaded_after_metadata": "ray" in sys.modules,
}
print("DJANGO_RAY_COLD_METADATA_PROBE=" + json.dumps(payload, sort_keys=True))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    marker = "DJANGO_RAY_COLD_METADATA_PROBE="
    payload_line = next(line for line in result.stdout.splitlines() if line.startswith(marker))
    assert json.loads(payload_line.removeprefix(marker)) == {
        "metadata": {},
        "ray_loaded_after_metadata": False,
        "ray_loaded_after_module_import": False,
        "ray_loaded_before": False,
    }


def test_ray_execution_metadata_does_not_initialize_local_ray() -> None:
    probe = """
import json

import ray

from django_ray.runtime.remote import _ray_execution_metadata

payload = {
    "initialized_before": ray.is_initialized(),
    "metadata": _ray_execution_metadata(),
    "initialized_after": ray.is_initialized(),
}
print("DJANGO_RAY_METADATA_PROBE=" + json.dumps(payload, sort_keys=True))
"""
    environment = os.environ.copy()
    environment.pop("RAY_ADDRESS", None)

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    marker = "DJANGO_RAY_METADATA_PROBE="
    payload_line = next(line for line in result.stdout.splitlines() if line.startswith(marker))
    assert json.loads(payload_line.removeprefix(marker)) == {
        "initialized_after": False,
        "initialized_before": False,
        "metadata": {},
    }
    combined_output = f"{result.stdout}\n{result.stderr}".lower()
    assert "local ray instance" not in combined_output


def test_progress_cost_timestamp_conversion_is_platform_independent() -> None:
    assert remote_module._event_timestamp_us("1970-01-01T00:00:00Z") == 0
    assert remote_module._event_timestamp_us("1969-12-31T23:59:59.999999Z") == -1
    assert (
        remote_module._event_timestamp_us("9999-12-31T23:59:59.999999Z") > 253_402_300_000_000_000
    )


@pytest.mark.parametrize("input_reference", [None, "private-input-reference"])
def test_unversioned_durable_core_carrier_is_inert(monkeypatch, input_reference):
    def forbidden(*args, **kwargs):
        pytest.fail("retired carrier reached application execution")

    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", forbidden)
    monkeypatch.setattr("django_ray.runtime.context.durable_task_execution", forbidden)
    result = execute_django_task_remote(
        "private.application.callback",
        "[]",
        "{}",
        44,
        attempt_number=1,
        execution_generation=1,
        input_reference=input_reference,
    )
    rejected = json.loads(result)
    assert rejected["error"] == "execution request rejected: legacy_request"
    assert rejected["retryable"] is False
    assert "private" not in result


@pytest.mark.parametrize("task_pk,run_identity", [(9, None), (None, {}), (9, {})])
def test_unbound_durable_workflow_leaf_refuses_before_application(
    monkeypatch, task_pk, run_identity
):
    def forbidden(*args, **kwargs):
        pytest.fail("unbound durable leaf reached application or progress handling")

    monkeypatch.setattr(remote_module, "_execute_workflow_step", forbidden)
    with pytest.raises(NestedExecutionRequestRejected) as raised:
        execute_workflow_step_remote(
            "private.canary",
            True,
            (),
            {},
            {},
            task_pk,
            object(),
            "0",
            workflow_run_identity=run_identity,
        )
    assert raised.value.classification is NestedExecutionRequestRejection.MISSING_CONTEXT


def test_standalone_workflow_leaf_remains_supported(monkeypatch):
    monkeypatch.setattr(remote_module, "_ray_execution_metadata", dict)
    assert (
        execute_workflow_step_remote(
            "tests.unit.test_remote.workflow_target",
            False,
            (),
            {"increment": 2},
            {},
            None,
            None,
            "0",
            3,
        )
        == 5
    )
