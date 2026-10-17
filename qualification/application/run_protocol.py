"""Prove current managers preserve and expose one unsupported queued execution."""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from qualification.application.api import (
    validate_execution_protocol_metrics,
    validate_execution_protocol_visibility,
    validate_task_status_payload,
)
from qualification.application.http_context import HttpObserver
from qualification.application.protocol_fixture import Fixture
from qualification.application.protocol_remote import Remote
from qualification.application.run_api import ApplicationHttp, read_token

LAYER = "application_protocol"
BUDGET_SECONDS = 120
WORK_SECONDS = 90
# The parent reserves four seconds to stop the owned process group. Leave one
# further second for receipt emission after these 25 seconds of child cleanup.
CLEANUP_SECONDS = 25
MOUNT_ROOT = Path("/rq2")
RECEIPT_ROOT = Path("/receipts")
FACTS = (
    "current_manager_heartbeat",
    "queued_row_and_history_unchanged",
    "api_protocol_visibility",
    "protocol_status_visibility",
    "fixed_metric_delta",
    "shared_worker_mount_verified",
    "strict_preinvocation_refused",
    "poison_marker_absent",
    "admission_restored",
    "owned_fixture_removed",
    "owned_remote_cleanup",
)
PHASES = frozenset(
    {
        "preflight",
        "bootstrap",
        "baseline",
        "seed",
        "shared_mount",
        "heartbeat",
        "strict_refusal",
        "task_status",
        "execution_list",
        "metrics",
        "protocol_status",
        "preservation",
        "remote_cleanup",
        "database_cleanup",
        "files_cleanup",
        "receipt",
    }
)
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "layer",
        "status",
        "complete_application_gate",
        "generation",
        "budget_seconds",
        "assertions",
        "failure_phase",
        "failure_type",
        "cleanup_failed",
    }
)


def validate_receipt(value):
    if (
        not isinstance(value, dict)
        or set(value) != RECEIPT_FIELDS
        or value.get("layer") != LAYER
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("generation") not in {"before", "after"}
        or type(value.get("budget_seconds")) is not int
        or value["budget_seconds"] != BUDGET_SECONDS
        or value.get("status") != "passed"
        or value.get("complete_application_gate") is not False
        or not isinstance(value.get("assertions"), dict)
        or set(value["assertions"]) != set(FACTS)
        or any(item is not True for item in value["assertions"].values())
        or value.get("failure_phase") is not None
        or value.get("failure_type") is not None
        or value.get("cleanup_failed") is not False
    ):
        raise ValueError("Incomplete owned protocol receipt")


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if not math.isfinite(seconds) or seconds <= 0:
        raise TimeoutError("Owned protocol phase expired")
    return seconds


def interrupt(_signum, _frame):
    raise TimeoutError("Owned protocol phase interrupted")


def arm(deadline):
    signal.setitimer(signal.ITIMER_REAL, remaining(deadline))


def remove_files(root, control):
    if not root.exists():
        return
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("Owned protocol directory identity changed")
    expected = {"control": control, "observed": control, "invoked": b"invoked"}
    entries = list(root.iterdir())
    if len(entries) > 3 or any(path.name not in expected for path in entries):
        raise ValueError("Unexpected file prevents protocol cleanup")
    for path in entries:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Unexpected file type prevents protocol cleanup")
        with path.open("rb") as stream:
            if stream.read(65) != expected[path.name]:
                raise ValueError("Owned protocol file content changed")
    for path in entries:
        path.unlink()
    root.rmdir()


def run(generation, base_url, token_file):
    started = time.monotonic()
    work_end = started + WORK_SECONDS
    phase_end = work_end + CLEANUP_SECONDS
    receipt = {
        "schema_version": 1,
        "layer": LAYER,
        "status": "failed",
        "complete_application_gate": False,
        "generation": generation,
        "budget_seconds": BUDGET_SECONDS,
        "assertions": dict.fromkeys(FACTS, False),
        "failure_phase": None,
        "failure_type": None,
        "cleanup_failed": False,
    }
    facts = receipt["assertions"]
    phase = "preflight"
    fixture = Fixture(generation)
    remote = Remote()
    root = MOUNT_ROOT / f"protocol-{generation}-{fixture.task_id}"
    control = secrets.token_hex(16).encode()
    root_created = False
    connection = None
    previous = {}

    def failed(error):
        if receipt["failure_phase"] is None:
            receipt["failure_phase"] = phase if phase in PHASES else "preflight"
            receipt["failure_type"] = (
                "timeout"
                if isinstance(error, TimeoutError)
                else "validation"
                if isinstance(error, ValueError)
                else "unavailable"
            )

    def checkpoint(deadline=work_end):
        seconds = remaining(deadline)
        if connection is not None:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    [f"{max(1, min(3000, int(seconds * 1000)))}ms"],
                )
                cursor.execute("SELECT set_config('lock_timeout', %s, false)", ["1000ms"])
        remaining(deadline)

    def request(path):
        checkpoint()
        client = ApplicationHttp(
            base_url,
            request_timeout=min(5, remaining(work_end)),
            observer=HttpObserver.API,
            observer_deadline=work_end,
        )
        status, body = client(
            path,
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
            response_limit=64 * 1024,
        )
        remaining(work_end)
        if status != 200:
            raise ValueError("Owned protocol HTTP observation failed")
        return body

    try:
        if (
            sys.platform != "linux"
            or generation not in {"before", "after"}
            or os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification"
            or MOUNT_ROOT.is_symlink()
            or not MOUNT_ROOT.is_dir()
            or MOUNT_ROOT.resolve() != MOUNT_ROOT
        ):
            raise ValueError("Protocol observer requires the owned Linux fixture")
        previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGTERM, signal.SIGALRM)}
        arm(work_end)
        phase = "bootstrap"
        import django

        django.setup()
        from django.db import connection as database

        if database.vendor != "postgresql" or database.in_atomic_block:
            raise ValueError("Protocol observer requires owned PostgreSQL")
        connection = database
        checkpoint()
        token = read_token(token_file)
        phase = "baseline"
        baseline = validate_execution_protocol_metrics(request("/api/metrics"))
        if baseline[("other", "QUEUED")] != 0:
            raise ValueError("Another unsupported queued fixture exists")
        root.mkdir(exist_ok=False)
        root_created = True
        (root / "control").write_bytes(control)
        phase = "seed"
        fixture.seed(checkpoint)
        phase = "shared_mount"
        remote.prepare(root, control, min(work_end, time.monotonic() + 30))
        facts["shared_worker_mount_verified"] = True
        phase = "heartbeat"
        heartbeat_end = min(work_end, time.monotonic() + 30)
        while True:
            checkpoint()
            if fixture.observe():
                break
            time.sleep(min(0.5, remaining(heartbeat_end)))
        facts["current_manager_heartbeat"] = True
        phase = "strict_refusal"
        remote.reject(fixture, min(work_end, time.monotonic() + 30))
        facts["strict_preinvocation_refused"] = True
        facts["poison_marker_absent"] = True
        phase = "task_status"
        payload = json.loads(request(f"/api/tasks/{fixture.task_id}"))
        if validate_task_status_payload(payload, task_id=fixture.task_id) != "QUEUED":
            raise ValueError("Unsupported task no longer appears queued")
        validate_execution_protocol_visibility(
            payload, surface="task status", expected_protocol=2, expected_compatible=False
        )
        phase = "execution_list"
        payload = json.loads(
            request("/api/executions?" + urlencode({"task_id": fixture.task_id, "limit": 1}))
        )
        tasks = payload.get("tasks")
        if not isinstance(tasks, list) or len(tasks) != 1:
            raise ValueError("Unsupported execution listing is not exact")
        if tasks[0].get("task_id") != fixture.task_id or tasks[0].get("state") != "QUEUED":
            raise ValueError("Execution listing returned another execution")
        validate_execution_protocol_visibility(
            tasks[0], surface="execution list", expected_protocol=2, expected_compatible=False
        )
        facts["api_protocol_visibility"] = True
        phase = "metrics"
        if validate_execution_protocol_metrics(request("/api/metrics"))[("other", "QUEUED")] != 1:
            raise ValueError("Unsupported fixed-label metric did not increase exactly once")
        facts["fixed_metric_delta"] = True
        phase = "protocol_status"
        checkpoint()
        fixture.status()
        facts["protocol_status_visibility"] = True
        phase = "preservation"
        checkpoint()
        if not fixture.observe():
            raise ValueError("Current manager heartbeat disappeared")
        facts["queued_row_and_history_unchanged"] = True
        remaining(work_end)
    except Exception as error:
        failed(error)
    finally:
        # Cleanup subslots are independent and capped by the original child
        # deadline. An uncertain remote task prevents deleting its sentinel.
        cleanup_end = min(phase_end, time.monotonic() + CLEANUP_SECONDS)
        remote_clean = False
        for name, allowance, action in (
            ("remote_cleanup", 7, lambda end: remote.cleanup(end)),
            ("database_cleanup", 12, lambda end: fixture.cleanup(lambda: checkpoint(end))),
        ):
            phase = name
            try:
                end = min(cleanup_end, time.monotonic() + allowance)
                if previous:
                    arm(end)
                action(end)
                remaining(end)
                if name == "remote_cleanup":
                    remote_clean = True
                    facts["owned_remote_cleanup"] = True
                elif fixture.revision is not None:
                    facts["admission_restored"] = True
            except Exception as error:
                receipt["cleanup_failed"] = True
                failed(error)
        phase = "files_cleanup"
        files_clean = False
        try:
            if previous:
                arm(cleanup_end)
            if root_created:
                if not remote_clean:
                    raise ValueError("Uncertain remote execution retains sentinel evidence")
                remove_files(root, control)
            remaining(cleanup_end)
            files_clean = not root.exists()
        except Exception as error:
            receipt["cleanup_failed"] = True
            failed(error)
        try:
            if previous:
                arm(cleanup_end)
            if connection is not None:
                connection.close()
            remaining(cleanup_end)
            if facts["admission_restored"] and files_clean:
                facts["owned_fixture_removed"] = True
        except Exception as error:
            receipt["cleanup_failed"] = True
            failed(error)
        if previous:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    if receipt["failure_phase"] is None and not receipt["cleanup_failed"]:
        receipt["status"] = "passed"
        validate_receipt(receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", required=True, choices=("before", "after"))
    parser.add_argument("--base-url", default="http://django-web:8000")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    receipt = run(args.generation, args.base_url, args.token_file)
    if receipt["status"] == "passed":
        try:
            if args.receipt != RECEIPT_ROOT / f"{args.generation}-protocol.json":
                raise ValueError("Unexpected protocol receipt destination")
            with args.receipt.open("x") as stream:
                stream.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        except Exception:
            receipt.update(status="failed", failure_phase="receipt", failure_type="unavailable")
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
