"""Retain a fixed nine-task reporting comparison inside disposable qualification."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

MAX_RECEIPT_BYTES = 512 * 1024
LAYER = "workflow_reporting_benchmark"


def validate_receipt(value):
    """Require the complete fixed policy matrix and acknowledged owned cleanup."""
    if not isinstance(value, dict) or not isinstance(value.get("report"), dict):
        raise ValueError("Reporting benchmark receipt is incomplete")
    report = value["report"]
    samples = report.get("samples", [])
    configuration = report.get("configuration", {})
    if (
        not isinstance(samples, list)
        or not all(isinstance(sample, dict) for sample in samples)
        or not isinstance(configuration, dict)
        or not isinstance(report.get("environment"), dict)
    ):
        raise ValueError("Reporting benchmark receipt is incomplete")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("layer") != LAYER
        or value.get("status") != "passed"
        or value.get("complete_application_gate") is not False
        or report.get("schema_version") != 4
        or report.get("benchmark") != "django-ray-live-workflow-reporting-policies"
        or configuration.get("repetitions") != 3
        or configuration.get("terminal_publisher") != "package_default"
        or report.get("environment", {}).get("database_vendor") != "postgresql"
        or len(samples) != 9
        or Counter(sample.get("policy") for sample in samples)
        != {"full": 3, "terminal_only": 3, "disabled": 3}
        or report.get("cleanup")
        != {
            "requested": True,
            "status": "completed",
            "execution_rows_deleted": 9,
            "retained_for_admin_inspection": False,
        }
    ):
        raise ValueError("Reporting benchmark receipt is incomplete")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = {
        "schema_version": 1,
        "layer": LAYER,
        "status": "failed",
        "complete_application_gate": False,
    }
    stage = "settings"
    try:
        if os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification":
            raise ValueError("Reporting benchmark requires disposable qualification settings")
        import django

        django.setup()
        from django.core.management import call_command

        os.environ["DJANGO_RAY_RUN_WORKFLOW_REPORTING_BENCHMARK"] = "1"
        # The command validates every sample and writes evidence before deleting
        # only its nine terminal executions. The outer Job supplies the deadline.
        stage = "execution"
        with open(os.devnull, "w") as quiet:
            call_command(
                "django_ray_benchmark_workflow_reporting",
                repetitions=3,
                fast_items=2,
                slow_items=1,
                fast_seconds=0.01,
                slow_seconds=0.02,
                timeout_seconds=45.0,
                poll_interval_seconds=0.25,
                cleanup=True,
                output_json=args.output,
                stdout=quiet,
                stderr=quiet,
            )
        stage = "artifact"
        with args.output.open("rb") as stream:
            raw = stream.read(MAX_RECEIPT_BYTES + 1)
        if len(raw) > MAX_RECEIPT_BYTES:
            raise ValueError("Reporting benchmark exceeds its artifact bound")
        receipt.update(status="passed", report=json.loads(raw))
        stage = "validation"
        validate_receipt(receipt)
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > MAX_RECEIPT_BYTES:
            raise ValueError("Reporting benchmark exceeds its receipt bound")
    except Exception as error:
        # Never export arbitrary exception text, credentials or response bodies.
        receipt.pop("report", None)
        receipt["status"] = "failed"
        receipt["failed_stage"] = stage
        cause: BaseException | None = error
        seen = set()
        for _ in range(8):
            if cause is None or id(cause) in seen:
                break
            seen.add(id(cause))
            trace = cause.__traceback__
            for _frame in range(64):
                if trace is None:
                    break
                if trace.tb_frame.f_globals.get("__name__") == (
                    "testproject.management.commands.django_ray_benchmark_workflow_reporting"
                ):
                    receipt["benchmark_failure_line"] = trace.tb_lineno
                trace = trace.tb_next
            cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
        encoded = json.dumps(receipt, sort_keys=True)
    print(encoded, flush=True)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
