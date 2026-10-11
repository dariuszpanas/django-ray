"""Observe real Jobs cancellation inside one owned Linux process namespace."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from qualification.cancellation.contract import (
    WORK_SECONDS,
    empty_receipt,
    record_failure,
    validate_receipt,
)
from qualification.cancellation.process_witness import bind_driver, bounded_bytes, driver_exited


def stop_manager(manager):
    if manager is None or manager.poll() is not None:
        raise ValueError("Owned manager exited before cleanup")
    manager.terminate()
    try:
        code = manager.wait(timeout=5)
    except subprocess.TimeoutExpired:
        manager.kill()
        manager.wait(timeout=2)
        raise ValueError("Owned manager required forced cleanup") from None
    if code != 128 + signal.SIGTERM:
        raise ValueError("Owned manager did not stop cleanly")


def identity(row):
    return (
        str(row.task_id),
        row.attempt_number,
        row.execution_generation,
        row.execution_protocol_version,
        row.ray_job_id,
        row.ray_job_request_reference,
    )


def execute(expected_module):
    receipt = empty_receipt()
    facts = receipt["facts"]
    phase = "bootstrap"
    manager = None
    ray = None
    deadline = time.monotonic() + WORK_SECONDS
    try:
        import django

        import django_ray

        if (
            not __debug__
            or sys.platform != "linux"
            or Path(django_ray.__file__).resolve() != Path(expected_module)
        ):
            raise ValueError("Probe requires the exact installed Linux candidate")
        django.setup()
        import ray
        from django.conf import settings
        from django.core.management import call_command

        from django_ray.lifecycle import request_task_cancellation
        from django_ray.models import CancellationStatus, RayTaskExecution, TaskAttempt, TaskState
        from django_ray.runner.ray_job import _address_pinned_job_client, _bounded_control_requests
        from qualification.cancellation.tasks import controlled

        call_command("migrate", verbosity=0, interactive=False)
        facts["installed_candidate"] = True
        phase = "runtime"
        if ray.is_initialized():
            raise ValueError("Probe cannot reuse an initialized runtime")
        ray.init(
            address="local",
            num_cpus=2,
            num_gpus=0,
            object_store_memory=128 * 1024 * 1024,
            include_dashboard=True,
            dashboard_host="127.0.0.1",
            dashboard_port=8265,
            log_to_driver=False,
        )
        client = _address_pinned_job_client("http://127.0.0.1:8265")
        phase = "manager"
        manager = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "django",
                "django_ray_worker",
                "--queue",
                "cancellation",
                "--concurrency",
                "1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def wait(predicate):
            while time.monotonic() < deadline:
                if manager.poll() is not None:
                    raise ValueError("Owned manager exited during observation")
                if result := predicate():
                    return result
                time.sleep(0.1)
            raise TimeoutError("Cancellation observation exhausted its work budget")

        def current(pk):
            return RayTaskExecution.objects.get(pk=pk)

        def job_state(row):
            with _bounded_control_requests(client):
                return str(client.get_job_info(row.ray_job_id).status)

        def run_case(case):
            nonlocal phase
            phase = f"{case}_start"
            root = settings.ROOT / case
            root.mkdir()
            nonce = uuid4().hex
            result = controlled.using(backend="cancellation", queue_name="cancellation").enqueue(
                case, nonce
            )
            row = RayTaskExecution.objects.get(task_id=result.id)
            wait(lambda: (root / "started.json").is_file())
            row = current(row.pk)
            original = identity(row)
            execution = [row.pk, str(row.task_id), row.attempt_number, row.execution_generation]
            driver = bind_driver(root / "started.json", nonce=nonce, case=case, execution=execution)
            if (
                row.state != TaskState.RUNNING
                or not row.ray_job_id
                or not row.ray_job_request_reference
                or job_state(row) != "RUNNING"
            ):
                raise ValueError("Owned request did not start as a running Job")
            progress = wait(
                lambda: (root / "progress").is_file() and len(bounded_bytes(root / "progress")) >= 2
            )
            if progress is not True or driver_exited(driver):
                raise ValueError("Owned driver did not progress while alive")
            facts[f"{case}_started_and_progressed"] = True
            before_progress = bounded_bytes(root / "progress")
            if case == "control":
                wait(lambda: len(bounded_bytes(root / "progress")) > len(before_progress))
                if driver_exited(driver) or job_state(row) != "RUNNING":
                    raise ValueError("No-cancel control stopped without release")
                facts["control_alive_without_cancel"] = True
                phase = "control_release"
                (root / "release").touch(exist_ok=False)
                wait(lambda: current(row.pk).state == TaskState.SUCCEEDED)
                wait(lambda: job_state(row) == "SUCCEEDED")
                wait(lambda: driver_exited(driver))
                completed = current(row.pk)
                if (
                    not (root / "completed").is_file()
                    or (root / "expired").exists()
                    or identity(completed) != original
                    or json.loads(completed.result_data) != {"control": "completed"}
                ):
                    raise ValueError("No-cancel control did not complete the same request")
                facts["control_completed_after_release"] = True
                facts["control_driver_exited"] = True
                return

            phase = "cancel_request"
            outcome = request_task_cancellation(
                row.pk,
                expected_attempt_number=row.attempt_number,
                expected_execution_generation=row.execution_generation,
            )
            if not outcome.accepted or outcome.state != TaskState.CANCELLING:
                raise ValueError("Running cancellation was not accepted")
            facts["cancellation_accepted"] = True
            phase = "cancel_observe"
            wait(lambda: current(row.pk).state == TaskState.CANCELLED)
            cancelled = current(row.pk)
            if cancelled.cancellation_status != CancellationStatus.REQUESTED:
                raise ValueError("Manager did not report a requested backend cancellation")
            facts["durable_cancelled_with_requested_status"] = True
            if identity(cancelled) != original:
                raise ValueError("Cancellation replaced the owned request")
            facts["same_request_attempt_generation"] = True
            wait(lambda: job_state(row) == "STOPPED")
            facts["ray_job_stopped"] = True
            wait(lambda: driver_exited(driver))
            facts["original_driver_exited"] = True
            frozen_progress = bounded_bytes(root / "progress")
            observe_until = time.monotonic() + 4

            def stable_terminal():
                final = current(row.pk)
                attempt = TaskAttempt.objects.get(execution=final)
                if (
                    identity(final) != original
                    or final.state != TaskState.CANCELLED
                    or final.cancellation_status != CancellationStatus.REQUESTED
                    or final.finished_at is None
                    or final.run_after is not None
                    or any(
                        value is not None
                        for value in (
                            final.completion_data,
                            final.result_data,
                            final.result_reference,
                            attempt.result_data,
                            attempt.result_reference,
                        )
                    )
                    or attempt.state != TaskState.CANCELLED
                    or attempt.attempt_number != final.attempt_number
                    or bounded_bytes(root / "progress") != frozen_progress
                    or any((root / name).exists() for name in ("release", "completed", "expired"))
                    or not driver_exited(driver)
                    or job_state(row) != "STOPPED"
                ):
                    raise ValueError("Observed cancellation lost its terminal boundary")
                return time.monotonic() >= observe_until

            wait(stable_terminal)
            facts["progress_stable_after_exit"] = True
            facts["one_cancelled_attempt_without_result_or_retry"] = True

        for case in ("control", "cancel"):
            run_case(case)
    except Exception:
        record_failure(receipt, phase)
    finally:
        signal.alarm(0)  # The outer 180-second child deadline still owns cleanup.
        try:
            stop_manager(manager)
            facts["manager_stopped"] = True
        except Exception:
            record_failure(receipt, "cleanup", cleanup=True)
        try:
            import psutil

            children = psutil.Process().children(recursive=True)
            if ray is not None:
                ray.shutdown()
            _, alive = psutil.wait_procs(children, timeout=15)
            if any(process.status() != psutil.STATUS_ZOMBIE for process in alive):
                raise ValueError("Owned Ray children remained alive")
            facts["ray_children_stopped"] = True
        except Exception:
            record_failure(receipt, "cleanup", cleanup=True)
        try:
            from django.db import connections

            connections.close_all()
        except Exception:
            record_failure(receipt, "cleanup", cleanup=True)
    if receipt["failure_phase"] == "none":
        receipt["status"] = "passed"
        validate_receipt(receipt)
    return receipt


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Expected installed module and receipt")

    def expired(_signum, _frame):
        raise TimeoutError("Cancellation work budget expired")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(WORK_SECONDS)
    try:
        receipt = execute(sys.argv[1])
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    with Path(sys.argv[2]).open("x") as stream:
        json.dump(receipt, stream, sort_keys=True)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
