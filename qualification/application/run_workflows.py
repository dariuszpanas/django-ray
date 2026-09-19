"""Observe five serial workflow fixtures through their API and Admin surfaces.

Run only inside the disposable application fixture. The outer workload owns the
hard deadline, source/cold-Ray proof, cancellation and namespace cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

from qualification.application.api import TASK_FAILURE_STATES, validate_task_status_payload
from qualification.application.run_api import ApplicationHttp, read_token
from qualification.application.workflow_http import decode_workflow_object, read_full_workflow_graph
from qualification.application.workflow_session import qualification_admin_session


@dataclass(frozen=True)
class WorkflowCase:
    name: str
    endpoint: str
    options: tuple[tuple[str, object], ...]
    states: tuple[str, ...]
    policy: str
    callable_path: str


def workflow_cases() -> tuple[WorkflowCase, ...]:
    """Use tiny existing public fixtures, not caller-selected application code."""
    cases = []
    for policy in ("full", "terminal_only"):
        for fail in (False, True):
            options: dict[str, object] = {
                "fast_items": 2,
                "slow_items": 1,
                "fast_seconds": 0.01,
                "slow_seconds": 0.05 if fail else 0.02,
            }
            if policy == "terminal_only":
                options["reporting_policy"] = policy
            if fail:
                options.update(failure_branch="slow", failure_item=0)
            cases.append(
                WorkflowCase(
                    f"{policy}-{'failure' if fail else 'success'}",
                    "/api/cluster/complex-workflow",
                    tuple(options.items()),
                    ("FAILED" if fail else "SUCCEEDED",),
                    policy,
                    "testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark",
                )
            )
    cases.append(
        WorkflowCase(
            "recovery",
            "/api/cluster/workflow-recovery-showcase",
            (("item_count", 1), ("work_seconds", 0.01)),
            ("FAILED", "FAILED", "SUCCEEDED"),
            "full",
            "testproject.apps.cluster_tasks.tasks.order_fulfillment_recovery_showcase_task",
        )
    )
    return tuple(cases)


def execute_case(request: ApplicationHttp, *, token: str, case: WorkflowCase) -> list[dict]:
    """Submit once, fail fast and inspect each retained attempt without replay."""
    from django.db.models.functions import Length

    from django_ray.models import RayTaskExecution

    if case not in workflow_cases():
        raise ValueError("Workflow qualification accepts only its fixed cases")
    headers = {"Authorization": f"Bearer {token}"}
    if case.name != "recovery":
        # Production deliberately disables the complex-workflow demo route.
        # Submit these fixed cases through the same bounded Django task API.
        from testproject.admission import enqueue_sample
        from testproject.apps.cluster_tasks.tasks import complex_workflow_benchmark

        result = enqueue_sample(complex_workflow_benchmark, **dict(case.options))
        enqueue = {"task_id": result.id, "args": result.args, "kwargs": result.kwargs}
    else:
        status, body = request(
            f"{case.endpoint}?{urlencode(dict(case.options))}",
            method="POST",
            headers=headers,
            response_limit=64 * 1024,
        )
        if status != 200:
            raise ValueError("Workflow fixture enqueue failed")
        enqueue = decode_workflow_object(body)
    task_id = enqueue.get("task_id")
    if not isinstance(task_id, str) or str(UUID(task_id)) != task_id or UUID(task_id).version != 4:
        raise ValueError("Workflow enqueue returned no canonical task identity")
    options = enqueue.get("kwargs")
    if (
        enqueue.get("args") != []
        or not isinstance(options, dict)
        or set(options) != dict(case.options).keys()
        or any(
            type(options[key]) is not type(value) or options[key] != value
            for key, value in case.options
        )
    ):
        raise ValueError("Workflow enqueue changed its bounded fixture inputs")
    deadline = time.monotonic() + 180
    while True:
        status, body = request(
            f"/api/tasks/{task_id}",
            method="GET",
            headers=headers,
            response_limit=64 * 1024,
            required_response_headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
        if status != 200:
            raise ValueError("Workflow fixture polling failed")
        state = validate_task_status_payload(decode_workflow_object(body), task_id=task_id)
        if state == case.states[-1]:
            break
        if state in TASK_FAILURE_STATES or time.monotonic() >= deadline:
            raise ValueError("Workflow fixture did not reach its expected terminal outcome")
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    row = RayTaskExecution.objects.only(
        "pk", "task_id", "state", "attempt_number", "callable_path"
    ).get(task_id=task_id)
    if (
        row.state != case.states[-1]
        or row.attempt_number != len(case.states)
        or row.callable_path != case.callable_path
    ):
        raise ValueError("Durable workflow differs from the requested fixture")
    attempts = list(
        row.attempts.annotate(summary_size=Length("workflow_progress_summary_json"))
        .filter(summary_size__lte=65536)
        .order_by("attempt_number")
        .values("attempt_number", "state", "workflow_progress_summary_json")[:4]
    )
    if [item["state"] for item in attempts] != list(case.states):
        raise ValueError("Workflow history is missing, oversized or has unexpected outcomes")
    if case.policy == "terminal_only":
        verify_no_workflow_detail(row.pk)
    admin_path = f"/admin/django_ray/raytaskexecution/{row.pk}/workflow/graph/"
    status, _ = request(admin_path, method="GET", response_limit=16 * 1024)
    if status not in {302, 403}:
        raise ValueError("Anonymous Admin graph access was not denied")
    status, _ = request(f"/api/cluster/workflows/{task_id}", method="GET", response_limit=16 * 1024)
    if status not in {401, 403}:
        raise ValueError("Anonymous workflow API access was not denied")
    observations = []
    with qualification_admin_session() as cookie:
        for number, attempt in enumerate(attempts, 1):
            if attempt["attempt_number"] != number:
                raise ValueError("Workflow history is not the exact attempt sequence")
            stored = attempt["workflow_progress_summary_json"]
            if not isinstance(stored, str) or len(stored.encode()) > 65536:
                raise ValueError("Workflow history summary is absent or unbounded")
            summary = decode_workflow_object(stored)
            identity = summary["run_identity"]
            if (
                identity.get("task_execution_pk") != row.pk
                or identity.get("attempt_number") != number
            ):
                raise ValueError("Stored workflow history belongs to another execution")
            public_identity = {
                key: identity[key]
                for key in ("schema_version", "run_id", "attempt_number", "execution_generation")
            }
            observations.append(
                read_full_workflow_graph(
                    request,
                    task_id=task_id,
                    execution_pk=row.pk,
                    run_identity=public_identity,
                    expected_state=attempt["state"],
                    token=token,
                    admin_cookie=cookie,
                    reporting_policy=case.policy,
                )
            )
    return observations


def verify_no_workflow_detail(execution_pk: int) -> None:
    """Check storage itself, including staged pages hidden by public readers."""
    from django_ray.models import (
        WorkflowProgressNodeDetail,
        WorkflowProgressTopologyManifest,
        WorkflowProgressTopologyPage,
    )

    for model in (
        WorkflowProgressNodeDetail,
        WorkflowProgressTopologyManifest,
        WorkflowProgressTopologyPage,
    ):
        if model.objects.filter(run_storage__execution_id=execution_pk).exists():
            raise ValueError("Terminal-only workflow retained graph storage")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://django-web:8000")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    receipt = {
        "schema_version": 1,
        "layer": "workflow_api_admin",
        "status": "failed",
        "complete_application_gate": False,
        "complete_workflow_gate": False,
        "failed_stage": "configuration",
    }
    try:
        if os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification":
            raise ValueError("Workflow assertions require disposable qualification settings")
        import django

        django.setup()
        from django_ray.conf.settings import get_settings

        if get_settings()["WORKFLOW_PROGRESS_SCHEMA_V3_PILOT"] is not True:
            raise ValueError("This baseline workload requires explicit pilot publication")
        request = ApplicationHttp(args.base_url)
        observations = {}
        for case in workflow_cases():
            receipt["failed_stage"] = case.name
            observations[case.name] = execute_case(
                request, token=read_token(args.token_file), case=case
            )
        receipt.update(
            status="passed", failed_stage=None, publisher="pilot", observations=observations
        )
        encoded = json.dumps(receipt, sort_keys=True).encode()
        if len(encoded) > 16384:
            raise ValueError("Workflow observation receipt exceeds its bound")
        with args.receipt.open("xb") as stream:
            stream.write(encoded)
    except Exception as error:
        if receipt["failed_stage"] is None:
            receipt["failed_stage"] = "receipt"
        receipt.update(status="failed")
        receipt.pop("observations", None)
        traceback = error.__traceback__
        while traceback is not None:
            module = traceback.tb_frame.f_globals.get("__name__", "")
            if module.startswith("qualification.application."):
                receipt["failed_location"] = {
                    "module": module,
                    "function": traceback.tb_frame.f_code.co_name,
                    "line": traceback.tb_lineno,
                }
            traceback = traceback.tb_next
    print(json.dumps(receipt, sort_keys=True), flush=True)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
