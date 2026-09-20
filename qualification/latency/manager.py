"""Run the actual manager loop with bounded counters and one explicit control."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

from qualification.latency.cost import ManagerCostSnapshots


def run(name: str, *, recovery_only: bool) -> None:
    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection, transaction
    from ray.job_submission import JobSubmissionClient

    import django_ray
    from django_ray.management.commands.django_ray_worker import Command

    counters = {
        "queries": 0,
        "query_seconds": 0.0,
        "fast_polls": 0,
        "fast_queries": 0,
        "fast_completed": 0,
        "reconciliations": 0,
        "peak_active": 0,
    }
    polling = False
    snapshots = ManagerCostSnapshots(settings.ROOT, name)
    processing_task_pk = None
    submit_job = JobSubmissionClient.submit_job

    def write_phase(kind, task_pk, value):
        with (settings.ROOT / f"{kind}-{task_pk}.json").open("x") as stream:
            json.dump(value, stream)

    def observed_submission(client, *args, **kwargs):
        assert type(processing_task_pk) is int
        started_ns = time.monotonic_ns()
        submission_id = submit_job(client, *args, **kwargs)
        acknowledged_ns = time.monotonic_ns()
        write_phase(
            "submission",
            processing_task_pk,
            {
                "job_id": submission_id,
                "submission_started_ns": started_ns,
                "submission_acknowledged_ns": acknowledged_ns,
            },
        )
        return submission_id

    def observe(execute, sql, params, many, context):
        started = time.monotonic()
        try:
            return execute(sql, params, many, context)
        finally:
            counters["queries"] += 1
            counters["query_seconds"] += time.monotonic() - started
            if polling:
                counters["fast_queries"] += 1

    class ObservedCommand(Command):
        def process_task(self, task):
            nonlocal processing_task_pk
            processing_task_pk = task.pk
            write_phase("claim", task.pk, {"claim_observed_ns": time.monotonic_ns()})
            try:
                return super().process_task(task)
            finally:
                processing_task_pk = None

        def _observe_persistence(self, method, task, *args, **kwargs):
            started_ns = time.monotonic_ns()
            persisted = method(task, *args, **kwargs)
            if persisted:
                transaction.on_commit(
                    lambda: write_phase(
                        "persistence",
                        task.pk,
                        {
                            "persistence_started_ns": started_ns,
                            "persistence_committed_ns": time.monotonic_ns(),
                        },
                    )
                )
            return persisted

        def _store_and_succeed_task(self, task, *args, **kwargs):
            return self._observe_persistence(super()._store_and_succeed_task, task, *args, **kwargs)

        def _handle_task_failure(self, task, *args, **kwargs):
            return self._observe_persistence(super()._handle_task_failure, task, *args, **kwargs)

        def poll_ray_job_completions(self):
            nonlocal polling
            snapshots.observe(self.worker_id, counters)
            counters["fast_polls"] += 1
            # This isolates the durable-receipt improvement in the same binary.
            # It is a recovery-only control, not an old-version benchmark.
            if recovery_only:
                return 0
            polling = True
            try:
                completed = super().poll_ray_job_completions()
                counters["fast_completed"] += completed
                return completed
            finally:
                polling = False

        def claim_and_process_tasks(self, queues, concurrency):
            # Fast polling stops when the final task leaves active_tasks.
            # Continue serving the final cost observation from the idle loop.
            snapshots.observe(self.worker_id, counters)
            result = super().claim_and_process_tasks(queues, concurrency)
            counters["peak_active"] = max(counters["peak_active"], len(self.active_tasks))
            return result

        def reconcile_tasks(self, queues=None):
            result = super().reconcile_tasks(queues)
            counters["reconciliations"] += 1
            if counters["reconciliations"] > 16:
                raise RuntimeError("qualification exceeded its bounded reconciliation observations")
            observation = {"worker_id": self.worker_id, "reconciled_ns": time.monotonic_ns()}
            scan = settings.ROOT / f"{name}-scan-{counters['reconciliations']:02d}.json"
            with scan.open("x") as stream:
                json.dump(observation, stream)
            ready = settings.ROOT / f"{name}-ready.json"
            if not ready.exists():
                with ready.open("x") as stream:
                    json.dump(observation, stream)
            return result

    command = ObservedCommand()
    started = time.monotonic()
    try:
        with (
            connection.execute_wrapper(observe),
            patch.object(JobSubmissionClient, "submit_job", observed_submission),
        ):
            call_command(command, queue="default", concurrency=1, verbosity=0)
    finally:
        with (settings.ROOT / f"{name}-metrics.json").open("x") as stream:
            json.dump(
                {
                    "counters": counters,
                    "recovery_only": recovery_only,
                    "module": str(Path(django_ray.__file__).resolve()),
                    "elapsed_seconds": time.monotonic() - started,
                    "shutdown_exit_code": command.shutdown_exit_code,
                },
                stream,
            )


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[2] not in ("fast", "recovery-only"):
        raise SystemExit("expected fixed manager name and polling mode")
    if not __debug__:
        raise SystemExit("latency qualification requires assertions enabled")
    run(sys.argv[1], recovery_only=sys.argv[2] == "recovery-only")
