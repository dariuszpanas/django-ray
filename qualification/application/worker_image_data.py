"""Fixed, bounded database observer for owned application-image worker checks."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
from pathlib import Path

MAX_ROWS = 512
MAX_STATE_BYTES = 131072


def state_path(owner: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{24}", owner):
        raise ValueError("Invalid image observation owner")
    return Path(f"/tmp/worker-image-{owner}.json")


def database_limits() -> None:
    from django.db import connection

    if connection.vendor != "postgresql":
        raise ValueError("Application image observation requires owned PostgreSQL")
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = '2000ms'")
        cursor.execute("SET lock_timeout = '1000ms'")


def snapshot() -> dict:
    """Hash full persisted rows in PostgreSQL without returning payload columns."""
    from django.db import connection

    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease

    if connection.vendor != "postgresql":
        raise ValueError("Application image observation requires owned PostgreSQL")
    result = {}
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = '2000ms'")
        cursor.execute("SET lock_timeout = '1000ms'")
        for model in (RayTaskExecution, TaskAttempt, TaskWorkerLease):
            table = connection.ops.quote_name(model._meta.db_table)
            key = connection.ops.quote_name(model._meta.pk.column)
            cursor.execute(
                f"SELECT LEFT({key}::text, 256), md5(row_to_json(t)::text) "
                f"FROM {table} t ORDER BY {key} LIMIT %s",
                [MAX_ROWS + 1],
            )
            rows = cursor.fetchall()
            if len(rows) > MAX_ROWS:
                raise ValueError("Application image history exceeds observation limit")
            result[model._meta.db_table] = [list(row) for row in rows]
    return result


def save(path: Path, value: dict) -> None:
    raw = json.dumps(value, sort_keys=True).encode()
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("Application image ownership state is oversized")
    temporary = path.with_suffix(".new")
    temporary.write_bytes(raw)
    temporary.replace(path)


def read(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("Application image ownership state is oversized")
    return json.loads(raw)


def run(owner: str, action: str, mode: str | None = None, hostname: str | None = None) -> dict:
    from django.db import transaction

    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease
    from testproject.tasks import add_numbers

    path = state_path(owner)
    prefix = f"mode-{owner}-"
    # This is an independent short-lived observer connection, not a manager's.
    database_limits()
    if action == "initialize":
        if path.exists() or TaskWorkerLease.objects.filter(hostname__startswith=prefix).exists():
            raise ValueError("Image observation owner already exists")
        if TaskWorkerLease.objects.filter(is_active=True).exists():
            raise ValueError("An application manager is still active")
        save(path, {"history": snapshot(), "ids": [], "modes": {}, "sentinel": None})
        return {"initialized": True}
    if action == "cleanup" and not path.exists():
        return {"rows_removed": True, "history_preserved": False}
    state = read(path)
    if action == "prepare":
        if mode not in {"sync", "ml"} or mode in state["modes"]:
            raise ValueError("Invalid image observation mode")
        with transaction.atomic():
            if state["sentinel"] is None:
                sentinel = add_numbers.using(queue_name="low-priority").enqueue(2, 3)
                state["ids"].append(sentinel.id)
                state["sentinel"] = {
                    "id": sentinel.id,
                    "row": json.dumps(
                        RayTaskExecution.objects.filter(task_id=sentinel.id).values().get(),
                        sort_keys=True,
                        default=str,
                    ),
                }
            selected = add_numbers.using(queue_name=mode).enqueue(2, 3)
            state["ids"].append(selected.id)
            state["modes"][mode] = selected.id
            # Save ownership before COMMIT: a lost exec response or transaction
            # rollback still leaves a safe cleanup list, never unknown rows.
            save(path, state)
        return {"prepared": True}
    if action == "cleanup":
        leases = TaskWorkerLease.objects.filter(hostname__startswith=prefix)
        if leases.filter(is_active=True).exists():
            raise ValueError("Owned worker lease is still active")
        RayTaskExecution.objects.filter(task_id__in=state["ids"]).delete()
        leases.delete()
        removed = not RayTaskExecution.objects.filter(task_id__in=state["ids"]).exists()
        preserved = snapshot() == state["history"]
        if not removed or not preserved:
            raise ValueError("Image observation cleanup or preservation failed")
        path.unlink()
        return {"rows_removed": True, "history_preserved": True}
    if mode is None or mode not in state["modes"] or action not in {"observe", "stopped"}:
        raise ValueError("Invalid image observation request")
    if (
        hostname is None
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]", hostname)
        or not hostname.startswith(prefix + mode + "-")
    ):
        raise ValueError("Invalid owned image Pod identity")
    sentinel = state["sentinel"]
    if (
        json.dumps(
            RayTaskExecution.objects.filter(task_id=sentinel["id"]).values().get(),
            sort_keys=True,
            default=str,
        )
        != sentinel["row"]
    ):
        raise ValueError("Unselected queue sentinel changed")
    row = RayTaskExecution.objects.get(task_id=state["modes"][mode])
    if row.state in {"QUEUED", "RUNNING"} and action == "observe":
        return {"complete": False}
    if (
        row.state != "SUCCEEDED"
        or row.attempt_number != 1
        or row.execution_generation != 1
        or row.result_reference is not None
        or row.result_data != "5"
    ):
        raise ValueError("Selected image task did not complete exactly once")
    attempt = TaskAttempt.objects.get(execution=row)
    if attempt.state != "SUCCEEDED" or attempt.attempt_number != 1:
        raise ValueError("Selected image attempt differs")
    lease = TaskWorkerLease.objects.get(worker_id=row.claimed_by_worker)
    if lease.hostname != hostname or lease.pid != 1 or lease.queue_name != mode:
        raise ValueError("Selected task belongs to another worker")
    if action == "stopped":
        if lease.is_active or lease.stopped_at is None:
            raise ValueError("Image manager did not release its lease gracefully")
        return {"inactive_lease": True, "sentinel_unchanged": True}
    if not lease.is_active:
        raise ValueError("Selected image manager lease is inactive")
    return {"complete": True, "one_attempt": True, "sentinel_unchanged": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("owner")
    parser.add_argument(
        "action", choices=("initialize", "prepare", "observe", "stopped", "cleanup")
    )
    parser.add_argument("--mode", choices=("sync", "ml"))
    parser.add_argument("--hostname")
    args = parser.parse_args(argv)
    signal.alarm(8)
    try:
        import django
        from django.db import connections

        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
            django.setup()
            try:
                result = run(args.owner, args.action, args.mode, args.hostname)
            finally:
                connections.close_all()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print('{"error":"image_worker_observation_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
