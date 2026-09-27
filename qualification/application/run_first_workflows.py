"""Compare matched first and warm workflows before other generation workloads.

The caller owns the fresh Ray generation, resource limits, hard deadline and
cleanup. First means before node-probe tasks and core smoke, not empty host caches.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from functools import partial
from pathlib import Path
from uuid import UUID

from qualification.application.resource_profiles import select_profile
from qualification.application.run_api import ApplicationHttp, read_token
from qualification.application.workflow_fixtures import FIRST_WORKFLOW_TRIAL_NAMES

LAYER = "first_workflow_pair"
RECEIPT_MAX_BYTES = 16 * 1024


def observe_pair(
    request: ApplicationHttp,
    *,
    token: str,
    resource_profile: str = "standard",
    validation_intent: str = "acceptance",
) -> dict:
    """Keep the first failure and submit a warm diagnostic only after settlement."""
    from qualification.application.run_workflows import execute_case, first_workflow_cases

    profile = select_profile(resource_profile, validation_intent)
    trials = []
    for case in first_workflow_cases():
        trial: dict = {"name": case.name, "status": "failed", "terminal_observed": False}

        started = time.monotonic()
        try:
            trial["observations"] = execute_case(
                request,
                token=token,
                case=case,
                on_terminal=partial(trial.update, terminal_observed=True),
            )
            trial["status"] = "passed"
        except Exception as error:
            # Export fixed source locations, never raw task errors or credentials.
            traceback = error.__traceback__
            while traceback is not None:
                module = traceback.tb_frame.f_globals.get("__name__", "")
                if module.startswith("qualification.application."):
                    trial["failure"] = {
                        "module": module,
                        "function": traceback.tb_frame.f_code.co_name,
                        "line": traceback.tb_lineno,
                    }
                traceback = traceback.tb_next
        trial["elapsed_seconds"] = round(time.monotonic() - started, 3)
        trials.append(trial)
        if not trial["terminal_observed"]:
            break
    return {
        "schema_version": 1,
        "layer": LAYER,
        "status": "passed"
        if len(trials) == 2 and all(trial["status"] == "passed" for trial in trials)
        else "failed",
        "complete_application_gate": False,
        "complete_workflow_gate": False,
        "ordering": "before_node_probe_tasks_and_core_smoke",
        "inputs": {"item_count": 3, "work_seconds": 0.05},
        "terminal_flush_timeout_seconds": 15,
        "qualification_profile": profile,
        "trials": trials,
    }


def validate_receipt(value: dict) -> None:
    """Refuse an unmatched pair or a warm success that hides the first failure."""
    profile = value.get("qualification_profile")
    if not isinstance(profile, dict) or profile != select_profile(
        profile.get("resource_profile"), profile.get("validation_intent")
    ):
        raise ValueError("First-workflow receipt has no verified profile selection")
    trials = value.get("trials")
    if (
        value.get("status") != "passed"
        or value.get("ordering") != "before_node_probe_tasks_and_core_smoke"
        or value.get("inputs") != {"item_count": 3, "work_seconds": 0.05}
        or type(value.get("terminal_flush_timeout_seconds")) is not int
        or value["terminal_flush_timeout_seconds"] != 15
        or not isinstance(trials, list)
        or len(trials) != 2
        or any(not isinstance(trial, dict) for trial in trials)
        or [trial.get("name") for trial in trials] != list(FIRST_WORKFLOW_TRIAL_NAMES)
    ):
        raise ValueError("First-workflow receipt does not contain the fixed matched pair")
    task_ids = set()
    for trial in trials:
        elapsed = trial.get("elapsed_seconds")
        if (
            trial.get("status") != "passed"
            or trial.get("terminal_observed") is not True
            or type(elapsed) not in {float, int}
            or not math.isfinite(elapsed)
            or elapsed < 0
            or not isinstance(trial.get("observations"), list)
            or len(trial["observations"]) != 1
        ):
            raise ValueError("First-workflow trial did not complete its observation")
        observation = trial["observations"][0]
        _require_fields(
            observation,
            {
                "state": "SUCCEEDED",
                "reporting_policy": "full",
                "counts": {"nodes": 21, "edges": 28},
                "api_admin_graph_match": True,
                "fixture_graph_verified": True,
                "complete_workflow_gate": False,
            },
        )
        task_id = observation.get("task_id")
        if (
            not isinstance(task_id, str)
            or str(UUID(task_id)) != task_id
            or UUID(task_id).version != 4
            or task_id in task_ids
        ):
            raise ValueError("First and warm workflows need distinct canonical task identities")
        task_ids.add(task_id)
        _require_fields(
            observation.get("admin_contract"),
            {
                "task_id": task_id,
                "task_state": "SUCCEEDED",
                "attempt_number": 1,
                "admin_workflow": "verified",
                "graph_status": "AVAILABLE",
                "graph_nodes": 21,
                "graph_edges": 28,
                "graph_succeeded_nodes": 21,
                "graph_preview_contract": "showcase-succeeded-verified",
                "diagnostics_preserved": True,
            },
        )
        _require_fields(
            observation.get("browser_contract"),
            {
                "status": "passed",
                "policy": "full",
                "attempts_rendered": 1,
                "javascript_errors": 0,
                "diagnostics_preserved": True,
            },
        )


def _require_fields(value: object, expected: dict) -> None:
    if not isinstance(value, dict) or any(
        type(value.get(key)) is not type(wanted) or value[key] != wanted
        for key, wanted in expected.items()
    ):
        raise ValueError("First-workflow receipt lacks its verified graph or rendered evidence")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://django-web:8000")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    receipt = {
        "schema_version": 1,
        "layer": LAYER,
        "status": "failed",
        "complete_application_gate": False,
        "complete_workflow_gate": False,
        "failed_stage": "configuration",
    }
    try:
        profile = select_profile(
            os.environ.get("DJANGO_RAY_QUALIFICATION_RESOURCE_PROFILE", "standard"),
            os.environ.get("DJANGO_RAY_QUALIFICATION_VALIDATION_INTENT", "acceptance"),
        )
        receipt["qualification_profile"] = profile
        if os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification":
            raise ValueError("First workflows require disposable qualification settings")
        import django

        django.setup()
        from django_ray.conf.settings import get_settings

        config = get_settings()
        if (
            "WORKFLOW_PROGRESS_SCHEMA_V3_PILOT" in config
            or config["WORKFLOW_PROGRESS_REPORTING_POLICY"] != "full"
            or config["WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS"] != 15
        ):
            raise ValueError("First workflows require the unchanged reporting defaults")
        receipt = observe_pair(
            ApplicationHttp(args.base_url),
            token=read_token(args.token_file),
            resource_profile=profile["resource_profile"],
            validation_intent=profile["validation_intent"],
        )
        if receipt["status"] == "passed":
            validate_receipt(receipt)
        encoded = json.dumps(receipt, sort_keys=True).encode()
        if len(encoded) > RECEIPT_MAX_BYTES:
            raise ValueError("First-workflow receipt exceeds its byte limit")
        with args.receipt.open("xb") as stream:
            stream.write(encoded)
    except Exception:
        receipt = {
            "schema_version": 1,
            "layer": LAYER,
            "status": "failed",
            "complete_application_gate": False,
            "complete_workflow_gate": False,
            "failed_stage": receipt.get("failed_stage", "receipt"),
            "qualification_profile": receipt.get("qualification_profile"),
        }
    print(json.dumps(receipt, sort_keys=True), flush=True)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
