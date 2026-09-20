"""Observe retired carrier refusal before setup in a fresh candidate process."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import patch


def verify_retired_carriers() -> dict[str, object]:
    import django
    from django.apps import apps

    from django_ray.execution_codec import (
        ExecutionIdentity,
        ExecutionRequest,
        NestedExecutionRequestRejected,
        NestedExecutionRequestRejection,
        encode_execution_request,
    )
    from django_ray.ray_job_protocol import (
        RAY_JOB_CONFIG_JSON_ENV_VAR,
        build_ray_job_request_metadata,
    )
    from django_ray.runtime import context, entrypoint, import_utils, remote, runtime_env

    assert not apps.ready, "carrier refusal must precede Django setup"
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("retired carrier crossed the application boundary")

    def check(value: str, classification: str = "legacy_request") -> None:
        decoded = json.loads(value)
        assert decoded["success"] is False
        assert decoded["retryable"] is False
        assert decoded["exception_type"] == "RayExecutionRequestIncompatible"
        assert decoded["error"] == f"execution request rejected: {classification}"
        assert decoded["traceback"] is None
        assert "carrier-private-canary" not in value

    with contextlib.ExitStack() as stack:
        for owner, name in (
            (django, "setup"),
            (runtime_env, "resolve_runtime_env_profile"),
            (entrypoint, "execute_task"),
            (entrypoint, "bootstrap_django"),
            (entrypoint, "load_task_input"),
            (entrypoint, "_invoke_task_callable"),
            (entrypoint, "_persist_task_completion"),
            (import_utils, "import_callable"),
            (context, "durable_task_execution"),
            (remote, "_execute_workflow_step"),
        ):
            stack.enter_context(patch.object(owner, name, forbidden))
        stack.enter_context(patch.dict(os.environ))
        for profile in (None, ""):
            for digest in (None, ""):
                legacy = SimpleNamespace(
                    pk=44,
                    runtime_env_profile=profile,
                    runtime_env_json="{}",
                    runtime_env_hash=digest,
                )
                try:
                    runtime_env.runtime_env_for_execution(legacy)
                except runtime_env.RuntimeEnvSnapshotError as error:
                    assert str(error) == (
                        "django-ray: Legacy RuntimeEnv snapshot cannot execute; enqueue a new task"
                    )
                else:
                    raise AssertionError("legacy RuntimeEnv snapshot was accepted")
        for metadata in (None, {"django_ray_task_id": "44", "django_ray_attempt_number": "1"}):
            if metadata is None:
                os.environ.pop(RAY_JOB_CONFIG_JSON_ENV_VAR, None)
            else:
                os.environ[RAY_JOB_CONFIG_JSON_ENV_VAR] = json.dumps({"metadata": metadata})
            for transport in (1, 2):
                payload = {
                    "callable_path": "carrier-private-canary.callback",
                    "task_execution_pk": 44,
                    "task_id": "carrier-private-canary",
                    "attempt_number": 1,
                    "execution_generation": 1,
                    "serialized_args": "[]",
                    "serialized_kwargs": "{}",
                    "transport_version": transport,
                    "input_reference": "carrier-private-canary" if transport == 2 else None,
                }
                encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
                check(entrypoint.execute_task_from_payload(encoded))
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    assert entrypoint.main(["--payload-b64", encoded]) == 78
                assert not output.getvalue()
                assert "carrier-private-canary" not in errors.getvalue()
        for transport in (1, 2):
            request = ExecutionRequest(
                identity=ExecutionIdentity(44, "retired-inline-task", 1, 1),
                execution_protocol_version=1,
                callable_path="carrier-private-canary.callback",
                transport_version=transport,
                serialized_args="[]" if transport == 1 else "null",
                serialized_kwargs="{}" if transport == 1 else "null",
                input_reference=None
                if transport == 1
                else "s3://carrier-private-canary/input.json",
                runtime_env_profile=None,
                runtime_env_hash="0" * 64,
                runtime_env_plan_identity={},
                compiled_graph_submission_transport="ray-job",
            )
            serialized = encode_execution_request(request)
            encoded = base64.urlsafe_b64encode(serialized.encode()).decode()
            os.environ[RAY_JOB_CONFIG_JSON_ENV_VAR] = json.dumps(
                {
                    "runtime_env": {},
                    "metadata": build_ray_job_request_metadata(request, serialized),
                }
            )
            check(entrypoint.execute_task_from_payload(encoded), "unsupported_transport")
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                assert entrypoint.main(["--payload-b64", encoded]) == 78
            assert not output.getvalue()
            assert "carrier-private-canary" not in errors.getvalue()
        os.environ.pop(RAY_JOB_CONFIG_JSON_ENV_VAR, None)
        check(entrypoint.execute_task_from_payload("%%%"), "invalid_versioned")
        for reference in (None, "carrier-private-canary"):
            check(
                remote.execute_django_task_remote(
                    "carrier-private-canary.callback",
                    "[]",
                    "{}",
                    44,
                    attempt_number=1,
                    execution_generation=1,
                    input_reference=reference,
                )
            )
        for task_pk, run_identity in ((44, None), (None, {}), (44, {})):
            try:
                remote.execute_workflow_step_remote(
                    "carrier-private-canary.callback",
                    True,
                    (),
                    {},
                    {},
                    task_pk,
                    None,
                    "0",
                    workflow_run_identity=run_identity,
                )
            except NestedExecutionRequestRejected as error:
                assert error.classification is NestedExecutionRequestRejection.MISSING_CONTEXT
                assert str(error) == "nested execution request rejected: missing_context"
            else:
                raise AssertionError("unbound durable workflow leaf was accepted")
    assert calls == 0
    assert not apps.ready
    return {
        "legacy_runtime_env_refused": 4,
        "job_carriers_refused": 4,
        "inline_job_carriers_refused": 2,
        "core_carriers_refused": 2,
        "workflow_carriers_refused": 3,
        "malformed_job_refused": True,
        "cli_refusal_exit": 78,
        "application_boundary_calls": 0,
        "django_setup": False,
    }
