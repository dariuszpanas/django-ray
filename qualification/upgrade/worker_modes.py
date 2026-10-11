"""Sequential installed sync and dedicated Core queue CLI observations."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

OBSERVATION_SECONDS = 60
CLEANUP_RESERVE_SECONDS = 15
MODES = ("sync", "core")
FACTS = frozenset(
    {
        "installed_package",
        "mode_witness",
        "dedicated_queue",
        "sentinel_unchanged",
        "one_attempt",
        "graceful_exit",
        "inactive_lease",
        "owned_rows_removed",
        "history_preserved",
    }
)


def command(mode: str) -> list[str]:
    if mode not in MODES:
        raise ValueError("Unsupported worker observation mode")
    return [
        sys.executable,
        "-m",
        "django",
        "django_ray_worker",
        "--sync" if mode == "sync" else "--cluster",
        *([] if mode == "sync" else ["auto"]),
        f"--queue=qualification-{mode}",
        "--concurrency=1",
    ]


def stop(process, deadline: float) -> None:
    """Signal only this still-owned direct child; forced exit is failed proof."""
    if process.poll() is not None:
        if process.returncode != 128 + signal.SIGINT:
            raise RuntimeError("Worker exited before graceful shutdown")
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=max(0.1, min(10, deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise RuntimeError("Worker required forced cleanup") from None
    if process.returncode != 128 + signal.SIGINT:
        raise RuntimeError("Worker did not exit gracefully")


def validate_result(value: object, *, mode: str, manager_pid: int, module: str) -> None:
    if (
        mode not in MODES
        or not isinstance(value, dict)
        or set(value) != {"value", "pid", "module", "ray_initialized"}
        or type(value["value"]) is not int
        or value["value"] != 42
        or type(value["pid"]) is not int
        or value["pid"] <= 0
        or value["module"] != module
        or value["ray_initialized"] is not (mode == "core")
        or (value["pid"] == manager_pid) is not (mode == "sync")
    ):
        raise ValueError("Installed worker mode result differs")


def validate_receipt(receipt: object) -> None:
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"schema_version", "modes", "deployment_equivalence"}
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["deployment_equivalence"] != "core-mode-only-not-all-manager-deployments"
        or not isinstance(receipt["modes"], dict)
        or set(receipt["modes"]) != set(MODES)
    ):
        raise ValueError("Incomplete worker mode receipt")
    for facts in receipt["modes"].values():
        if (
            not isinstance(facts, dict)
            or set(facts) != FACTS
            or any(value is not True for value in facts.values())
        ):
            raise ValueError("Incomplete worker mode facts")


def run(snapshot) -> dict:
    """Use the fixture's existing Ray/database within one observation budget.

    ORM/socket calls retain the enclosing fixture's finite connection/query
    limits; its existing 240-second process deadline is the hard outer bound.
    The sixty-second observation never grants another per-mode budget.
    """
    from django.db import connections

    import django_ray
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease
    from qualification.upgrade.mode_tasks import mode_value

    deadline = time.monotonic() + OBSERVATION_SECONDS
    module = str(Path(django_ray.__file__).resolve())
    history = snapshot()
    leases = list(TaskWorkerLease.objects.order_by("pk").values())
    assert not TaskWorkerLease.objects.filter(is_active=True).exists()
    completed = {}
    for mode in MODES:
        if deadline - time.monotonic() <= CLEANUP_RESERVE_SECONDS:
            raise TimeoutError("Worker mode observation budget exhausted")
        task_ids = []
        process = None
        new_leases = []
        try:
            # The older other-queue task must not be claimed by either manager.
            sentinel = mode_value.using(queue_name="qualification-unselected").enqueue()
            task_ids.append(sentinel.id)
            original = RayTaskExecution.objects.filter(task_id=sentinel.id).values().get()
            selected = mode_value.using(queue_name=f"qualification-{mode}").enqueue()
            task_ids.append(selected.id)
            queued = RayTaskExecution.objects.get(task_id=selected.id)
            assert queued.state == "QUEUED" and queued.execution_generation == 0
            if deadline - time.monotonic() <= CLEANUP_RESERVE_SECONDS:
                raise TimeoutError("Insufficient cleanup reserve to start a worker")
            process = subprocess.Popen(
                command(mode), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Worker exited before completing the selected task")
                assert (
                    RayTaskExecution.objects.filter(task_id=sentinel.id).values().get() == original
                )
                row = RayTaskExecution.objects.get(task_id=selected.id)
                if row.state in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST", "EXPIRED"}:
                    break
                if deadline - time.monotonic() <= CLEANUP_RESERVE_SECONDS:
                    raise TimeoutError("Worker mode task observation timed out")
                time.sleep(0.1)
            assert row.state == "SUCCEEDED"
            assert row.attempt_number == 1 and row.execution_generation == 1
            assert row.result_reference is None and row.result_data is not None
            assert len(row.result_data.encode("utf-8")) <= 4096
            validate_result(
                json.loads(row.result_data), mode=mode, manager_pid=process.pid, module=module
            )
            lease = TaskWorkerLease.objects.get(worker_id=row.claimed_by_worker)
            new_leases.append(lease.pk)
            assert lease.pid == process.pid and lease.queue_name == f"qualification-{mode}"
            assert lease.is_active is True
            assert TaskAttempt.objects.filter(execution=row).count() == 1
            attempt = TaskAttempt.objects.get(execution=row)
            assert attempt.attempt_number == 1 and attempt.state == "SUCCEEDED"
            stop(process, deadline)
            lease.refresh_from_db()
            assert lease.is_active is False
            assert RayTaskExecution.objects.filter(task_id=sentinel.id).values().get() == original
        finally:
            try:
                if process is not None:
                    stop(process, deadline)
            finally:
                RayTaskExecution.objects.filter(task_id__in=task_ids).delete()
                # A failed task may still have created its owned lease before
                # the success checks could identify it. Never delete old rows.
                if process is not None:
                    new_leases.extend(
                        TaskWorkerLease.objects.filter(
                            pid=process.pid, queue_name=f"qualification-{mode}"
                        )
                        .exclude(pk__in=[lease["worker_id"] for lease in leases])
                        .values_list("pk", flat=True)
                    )
                TaskWorkerLease.objects.filter(pk__in=new_leases).delete()
        assert not RayTaskExecution.objects.filter(task_id__in=task_ids).exists()
        assert snapshot() == history
        assert list(TaskWorkerLease.objects.order_by("pk").values()) == leases
        if time.monotonic() > deadline:
            raise TimeoutError("Worker mode observation exceeded its budget")
        completed[mode] = dict.fromkeys(FACTS, True)
    connections.close_all()
    receipt = {
        "schema_version": 1,
        "modes": completed,
        "deployment_equivalence": "core-mode-only-not-all-manager-deployments",
    }
    validate_receipt(receipt)
    return receipt
