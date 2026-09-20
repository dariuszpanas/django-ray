"""Observe retired carrier refusal before setup in a fresh candidate process."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
from unittest.mock import patch


def verify_retired_carriers() -> dict[str, object]:
    import django
    from django.apps import apps

    from django_ray.ray_job_protocol import RAY_JOB_CONFIG_JSON_ENV_VAR
    from django_ray.runtime import context, entrypoint, import_utils, remote

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
            (entrypoint, "execute_task"),
            (entrypoint, "bootstrap_django"),
            (entrypoint, "load_task_input"),
            (entrypoint, "_invoke_task_callable"),
            (entrypoint, "_persist_task_completion"),
            (import_utils, "import_callable"),
            (context, "durable_task_execution"),
        ):
            stack.enter_context(patch.object(owner, name, forbidden))
        stack.enter_context(patch.dict(os.environ))
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
    assert calls == 0
    assert not apps.ready
    return {
        "job_carriers_refused": 4,
        "core_carriers_refused": 2,
        "malformed_job_refused": True,
        "cli_refusal_exit": 78,
        "application_boundary_calls": 0,
        "django_setup": False,
    }
