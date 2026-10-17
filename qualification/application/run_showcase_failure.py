"""Run the fixed reservation failure in its own finite generation phase."""

from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path

from qualification.application.http_context import HttpObserver
from qualification.application.run_api import ApplicationHttp, ApplicationHttpError, read_token
from qualification.application.showcase_failure import validate_observation

LAYER = "application_showcase_failure"
RECEIPT_MAX_BYTES = 8192
WORK_SECONDS = 150


def validate_receipt(value: object) -> None:
    """Reject a missing, partial, wrong-generation or merely declared success."""
    if not isinstance(value, dict):
        raise ValueError("Missing showcase failure receipt")
    expected = {
        "schema_version": 1,
        "layer": LAYER,
        "status": "passed",
        "complete_application_gate": False,
        "complete_workflow_gate": False,
        "failed_stage": None,
    }
    if any(type(value.get(k)) is not type(v) or value[k] != v for k, v in expected.items()):
        raise ValueError("Incomplete showcase failure receipt")
    if value.get("generation") not in {"before", "after"}:
        raise ValueError("Invalid showcase failure generation")
    validate_observation(value.get("observation"))


def observe(request: ApplicationHttp, token: str) -> dict:
    from qualification.application.run_workflows import execute_case, showcase_failure_case

    (observation,) = execute_case(request, token=token, case=showcase_failure_case())
    validate_observation(observation)
    return observation


def _expired(_signum, _frame):
    raise TimeoutError("Showcase failure work deadline expired")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", required=True, choices=("before", "after"))
    parser.add_argument("--base-url", default="http://django-web:8000")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    receipt = {
        "schema_version": 1,
        "layer": LAYER,
        "generation": args.generation,
        "status": "failed",
        "complete_application_gate": False,
        "complete_workflow_gate": False,
        "failed_stage": "bootstrap",
    }
    previous = None
    try:
        if os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification":
            raise ValueError("Showcase failure requires disposable qualification settings")
        # The parent reserves process-group shutdown inside the 180-second
        # phase. This work alarm leaves time for session/user context cleanup;
        # namespace teardown owns all task rows and remaining runtime resources.
        previous = signal.signal(signal.SIGALRM, _expired)
        signal.alarm(WORK_SECONDS)
        import django

        django.setup()
        from django_ray.conf.settings import get_settings

        config = get_settings()
        if (
            "WORKFLOW_PROGRESS_SCHEMA_V3_PILOT" in config
            or config["WORKFLOW_PROGRESS_REPORTING_POLICY"] != "full"
            or "testproject.apps.cluster_tasks.workflows.WorkflowShowcaseFixtureError"
            not in config["RETRY_EXCEPTION_DENYLIST"]
        ):
            raise ValueError(
                "Showcase failure requires unchanged full reporting and refusal policy"
            )
        receipt["failed_stage"] = "observe"
        observation = observe(
            ApplicationHttp(
                args.base_url, retry_read_disconnects=True, observer=HttpObserver.WORKFLOW
            ),
            read_token(args.token_file),
        )
        receipt.update(status="passed", failed_stage=None, observation=observation)
        validate_receipt(receipt)
        encoded = json.dumps(receipt, sort_keys=True).encode()
        if len(encoded) > RECEIPT_MAX_BYTES:
            raise ValueError("Showcase failure receipt exceeds its byte limit")
        with args.receipt.open("xb") as stream:
            stream.write(encoded)
    except Exception as error:
        receipt.pop("observation", None)
        receipt.update(status="failed", failed_stage=receipt["failed_stage"] or "receipt")
        receipt["failure_kind"] = "timeout" if isinstance(error, TimeoutError) else "validation"
        if isinstance(error, ApplicationHttpError):
            receipt["http_failure"] = error.diagnostic()
    finally:
        if previous is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
    print(json.dumps(receipt, sort_keys=True), flush=True)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
