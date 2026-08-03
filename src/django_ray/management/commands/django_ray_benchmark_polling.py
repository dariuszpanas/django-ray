"""Benchmark fixed and adaptive production worker polling against PostgreSQL."""

from __future__ import annotations

import json
import math
import platform
import random
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import asdict, dataclass
from io import StringIO
from typing import Any

import django
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import close_old_connections, connection
from django.db.migrations.recorder import MigrationRecorder

from django_ray.management.commands.django_ray_worker import Command as WorkerCommand
from django_ray.management.diagnostics import render_console_diagnostic
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner.leasing import WorkerLeaseIdentity, get_heartbeat_interval
from django_ray.runner.polling import AdaptivePollingPolicy


def _percentile(values: list[float], percentile: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _cross_worker_overlap_metrics(
    query_times_by_worker: list[list[float]],
    *,
    window_seconds: float,
) -> tuple[int, float]:
    """Return peak distinct-worker overlap and the overlapping-event ratio.

    The first observed claim query from each worker is its barrier-release query,
    so it is excluded deliberately. Remaining events overlap only when another
    worker queried within the configured sliding window; same-worker bursts never
    count as coordination collisions.
    """
    events = sorted(
        (timestamp, worker_index)
        for worker_index, worker_times in enumerate(query_times_by_worker)
        for timestamp in worker_times[1:]
    )
    if not events:
        return 0, 0.0

    active_workers: Counter[int] = Counter()
    active_event_indices: deque[int] = deque()
    overlapping_events: set[int] = set()
    peak_overlapping_workers = 0
    for event_index, (timestamp, worker_index) in enumerate(events):
        while (
            active_event_indices and timestamp - events[active_event_indices[0]][0] > window_seconds
        ):
            expired_index = active_event_indices.popleft()
            expired_worker = events[expired_index][1]
            active_workers[expired_worker] -= 1
            if active_workers[expired_worker] == 0:
                del active_workers[expired_worker]

        distinct_active_workers = set(active_workers)
        distinct_active_workers.add(worker_index)
        if len(distinct_active_workers) >= 2:
            peak_overlapping_workers = max(
                peak_overlapping_workers,
                len(distinct_active_workers),
            )
            for other_index in active_event_indices:
                if events[other_index][1] != worker_index:
                    overlapping_events.add(other_index)
                    overlapping_events.add(event_index)

        active_workers[worker_index] += 1
        active_event_indices.append(event_index)

    return peak_overlapping_workers, len(overlapping_events) / len(events)


def _is_claim_query(sql: str) -> bool:
    """Identify the production task claim SELECT from executed SQL text."""
    normalized = " ".join(sql.upper().split())
    table_name = RayTaskExecution._meta.db_table.upper()
    return (
        normalized.startswith("SELECT") and table_name in normalized and "FOR UPDATE" in normalized
    )


@dataclass(frozen=True)
class BenchmarkResult:
    """Metrics from one fixed or adaptive polling run."""

    policy: str
    workers: int
    tasks_per_phase: int
    base_interval_seconds: float
    max_interval_seconds: float
    idle_claim_queries_per_worker_second: float
    idle_total_sql_per_worker_second: float
    idle_peak_overlapping_workers: int
    idle_cross_worker_overlap_ratio: float
    claim_latency_p50_ms: float
    claim_latency_p95_ms: float
    burst_claim_throughput_per_second: float


@dataclass
class _ThreadMetrics:
    sql_query_times_by_worker: list[list[float]]
    claim_query_times_by_worker: list[list[float]]
    errors: list[str]
    lock: threading.Lock


@dataclass
class _WorkerGroup:
    stop: threading.Event
    ready: threading.Barrier
    threads: list[threading.Thread]
    metrics: _ThreadMetrics
    lease_identities: list[WorkerLeaseIdentity | None]


class Command(BaseCommand):
    """Compare fixed and adaptive django-ray worker claims on PostgreSQL."""

    help = "Benchmark fixed and adaptive production worker polling on PostgreSQL"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--workers", type=int, default=4)
        parser.add_argument("--tasks", type=int, default=100)
        parser.add_argument("--idle-seconds", type=float, default=2.0)
        parser.add_argument("--enqueue-interval-seconds", type=float, default=0.05)
        parser.add_argument("--base-interval-seconds", type=float, default=0.1)
        parser.add_argument("--max-interval-seconds", type=float, default=0.5)
        parser.add_argument("--overlap-window-ms", type=float, default=25.0)
        parser.add_argument("--seed", type=int, default=53)
        parser.add_argument("--barrier-timeout-seconds", type=float, default=10.0)
        parser.add_argument("--json", action="store_true", dest="json_output")

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        if connection.vendor != "postgresql":
            raise CommandError(
                "django_ray_benchmark_polling requires PostgreSQL so skip_locked and "
                "multi-worker query behavior match production"
            )

        workers = self._positive_int(options["workers"], "--workers")
        tasks = self._positive_int(options["tasks"], "--tasks")
        seed = self._integer(options["seed"], "--seed")
        idle_seconds = self._positive_float(options["idle_seconds"], "--idle-seconds")
        enqueue_interval = self._positive_float(
            options["enqueue_interval_seconds"], "--enqueue-interval-seconds"
        )
        base_interval = self._positive_float(
            options["base_interval_seconds"], "--base-interval-seconds"
        )
        max_interval = self._positive_float(
            options["max_interval_seconds"], "--max-interval-seconds"
        )
        overlap_window = (
            self._positive_float(options["overlap_window_ms"], "--overlap-window-ms") / 1000
        )
        barrier_timeout = self._positive_float(
            options["barrier_timeout_seconds"], "--barrier-timeout-seconds"
        )
        if max_interval < base_interval:
            raise CommandError("--max-interval-seconds must be at least --base-interval-seconds")

        results = [
            self._run_policy(
                policy_name="fixed",
                workers=workers,
                task_count=tasks,
                idle_seconds=idle_seconds,
                enqueue_interval=enqueue_interval,
                base_interval=base_interval,
                max_interval=base_interval,
                jitter_ratio=0.0,
                overlap_window=overlap_window,
                barrier_timeout=barrier_timeout,
                seed=seed,
            ),
            self._run_policy(
                policy_name="adaptive",
                workers=workers,
                task_count=tasks,
                idle_seconds=idle_seconds,
                enqueue_interval=enqueue_interval,
                base_interval=base_interval,
                max_interval=max_interval,
                jitter_ratio=0.2,
                overlap_window=overlap_window,
                barrier_timeout=barrier_timeout,
                seed=seed,
            ),
        ]

        payload = {
            "environment": self._environment_metadata(
                workers=workers,
                tasks=tasks,
                idle_seconds=idle_seconds,
                enqueue_interval=enqueue_interval,
                overlap_window=overlap_window,
                seed=seed,
            ),
            "results": [asdict(result) for result in results],
        }
        if options["json_output"]:
            self.stdout.write(json.dumps(payload, sort_keys=True))
            return

        for result in results:
            self.stdout.write(
                f"{result.policy}: idle_claim_queries="
                f"{result.idle_claim_queries_per_worker_second:.2f} per worker/s, "
                f"idle_total_sql={result.idle_total_sql_per_worker_second:.2f} per worker/s, "
                f"peak_overlapping_workers={result.idle_peak_overlapping_workers}, "
                f"cross_worker_overlap_ratio={result.idle_cross_worker_overlap_ratio:.3f}, "
                f"claim_p50={result.claim_latency_p50_ms:.2f}ms, "
                f"claim_p95={result.claim_latency_p95_ms:.2f}ms, "
                f"burst_throughput={result.burst_claim_throughput_per_second:.2f} tasks/s"
            )

    @staticmethod
    def _positive_int(value: object, option: str) -> int:
        if type(value) is not int or value <= 0:
            raise CommandError(f"{option} must be a positive integer")
        return value

    @staticmethod
    def _integer(value: object, option: str) -> int:
        if type(value) is not int:
            raise CommandError(f"{option} must be an integer")
        return value

    @staticmethod
    def _positive_float(value: object, option: str) -> float:
        if type(value) not in (int, float):
            raise CommandError(f"{option} must be a positive number")
        numeric_value = float(value)
        if not math.isfinite(numeric_value) or numeric_value <= 0:
            raise CommandError(f"{option} must be a positive number")
        return numeric_value

    @staticmethod
    def _environment_metadata(
        *,
        workers: int,
        tasks: int,
        idle_seconds: float,
        enqueue_interval: float,
        overlap_window: float,
        seed: int,
    ) -> dict[str, object]:
        return {
            "database": connection.vendor,
            "database_server_version": str(getattr(connection, "pg_version", "unknown")),
            "django_ray_schema_version": Command._schema_version(),
            "django_version": django.get_version(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "workers": workers,
            "tasks_per_phase": tasks,
            "idle_seconds": idle_seconds,
            "enqueue_interval_seconds": enqueue_interval,
            "overlap_window_ms": overlap_window * 1000,
            "seed": seed,
        }

    @staticmethod
    def _schema_version() -> str:
        applied = sorted(
            name
            for app, name in MigrationRecorder(connection).applied_migrations()
            if app == "django_ray"
        )
        return applied[-1] if applied else "unmigrated"

    def _run_policy(
        self,
        *,
        policy_name: str,
        workers: int,
        task_count: int,
        idle_seconds: float,
        enqueue_interval: float,
        base_interval: float,
        max_interval: float,
        jitter_ratio: float,
        overlap_window: float,
        barrier_timeout: float,
        seed: int,
    ) -> BenchmarkResult:
        idle_metrics, claim_latencies, idle_elapsed = self._run_idle_and_latency_phase(
            policy_name=policy_name,
            workers=workers,
            task_count=task_count,
            idle_seconds=idle_seconds,
            enqueue_interval=enqueue_interval,
            base_interval=base_interval,
            max_interval=max_interval,
            jitter_ratio=jitter_ratio,
            barrier_timeout=barrier_timeout,
            seed=seed,
        )
        throughput = self._run_throughput_phase(
            policy_name=policy_name,
            workers=workers,
            task_count=task_count,
            base_interval=base_interval,
            max_interval=max_interval,
            jitter_ratio=jitter_ratio,
            barrier_timeout=barrier_timeout,
            seed=seed,
        )
        idle_query_count = sum(len(times) for times in idle_metrics.claim_query_times_by_worker)
        idle_total_sql = sum(len(times) for times in idle_metrics.sql_query_times_by_worker)
        peak, overlap_ratio = _cross_worker_overlap_metrics(
            idle_metrics.claim_query_times_by_worker,
            window_seconds=overlap_window,
        )
        return BenchmarkResult(
            policy=policy_name,
            workers=workers,
            tasks_per_phase=task_count,
            base_interval_seconds=base_interval,
            max_interval_seconds=max_interval,
            idle_claim_queries_per_worker_second=idle_query_count / workers / idle_elapsed,
            idle_total_sql_per_worker_second=idle_total_sql / workers / idle_elapsed,
            idle_peak_overlapping_workers=peak,
            idle_cross_worker_overlap_ratio=overlap_ratio,
            claim_latency_p50_ms=_percentile(claim_latencies, 0.5) * 1000,
            claim_latency_p95_ms=_percentile(claim_latencies, 0.95) * 1000,
            burst_claim_throughput_per_second=throughput,
        )

    def _run_idle_and_latency_phase(
        self,
        *,
        policy_name: str,
        workers: int,
        task_count: int,
        idle_seconds: float,
        enqueue_interval: float,
        base_interval: float,
        max_interval: float,
        jitter_ratio: float,
        barrier_timeout: float,
        seed: int,
    ) -> tuple[_ThreadMetrics, list[float], float]:
        run_id = uuid.uuid4().hex
        queue_name = f"django-ray-poll-latency-{run_id}"
        task_prefix = f"poll-latency-{run_id}-"
        enqueue_times: dict[str, float] = {}
        claim_latencies: list[float] = []
        claimed_task_ids: list[str] = []
        done = threading.Event()
        lock = threading.Lock()

        def on_claim(task: RayTaskExecution) -> None:
            claimed_at = time.monotonic()
            with lock:
                enqueued_at = enqueue_times.get(str(task.task_id))
                if enqueued_at is not None:
                    claimed_task_ids.append(str(task.task_id))
                    claim_latencies.append(claimed_at - enqueued_at)
                    if len(claim_latencies) >= task_count:
                        done.set()

        group = self._start_workers(
            phase="latency",
            policy_name=policy_name,
            workers=workers,
            queue_name=queue_name,
            on_claim=on_claim,
            base_interval=base_interval,
            max_interval=max_interval,
            jitter_ratio=jitter_ratio,
            barrier_timeout=barrier_timeout,
            seed=seed,
        )
        try:
            idle_started = time.monotonic()
            self._release_workers(group, barrier_timeout=barrier_timeout)
            if group.stop.wait(idle_seconds):
                self._raise_worker_error(group, policy_name)
            idle_ended = time.monotonic()
            with group.metrics.lock:
                idle_metrics = _ThreadMetrics(
                    sql_query_times_by_worker=[
                        [
                            timestamp
                            for timestamp in times
                            if idle_started <= timestamp <= idle_ended
                        ]
                        for times in group.metrics.sql_query_times_by_worker
                    ],
                    claim_query_times_by_worker=[
                        [
                            timestamp
                            for timestamp in times
                            if idle_started <= timestamp <= idle_ended
                        ]
                        for times in group.metrics.claim_query_times_by_worker
                    ],
                    errors=list(group.metrics.errors),
                    lock=threading.Lock(),
                )

            for index in range(task_count):
                task_id = f"{task_prefix}{index}"
                with lock:
                    enqueue_times[task_id] = time.monotonic()
                self._create_execution(task_id=task_id, queue_name=queue_name)
                if index + 1 < task_count:
                    time.sleep(enqueue_interval)

            timeout = max(10.0, task_count * enqueue_interval * 2 + max_interval * 4)
            if not done.wait(timeout):
                self._raise_worker_error(group, policy_name)
                raise CommandError(
                    f"{policy_name} latency phase claimed {len(claim_latencies)}/{task_count} "
                    f"tasks before the {timeout:g}s timeout"
                )
            self._raise_worker_error(group, policy_name)
            self._assert_claim_integrity(
                task_prefix=task_prefix,
                claimed_task_ids=claimed_task_ids,
                worker_ids={
                    identity.worker_id
                    for identity in group.lease_identities
                    if identity is not None
                },
                expected_count=task_count,
                phase=f"{policy_name} latency",
            )
            return idle_metrics, claim_latencies, max(1e-9, idle_ended - idle_started)
        finally:
            self._stop_workers(group, max_interval=max_interval)
            self._cleanup_phase_rows(
                task_prefix=task_prefix,
                lease_identities=group.lease_identities,
            )

    def _run_throughput_phase(
        self,
        *,
        policy_name: str,
        workers: int,
        task_count: int,
        base_interval: float,
        max_interval: float,
        jitter_ratio: float,
        barrier_timeout: float,
        seed: int,
    ) -> float:
        run_id = uuid.uuid4().hex
        queue_name = f"django-ray-poll-throughput-{run_id}"
        task_prefix = f"poll-throughput-{run_id}-"
        claimed_at: list[float] = []
        claimed_task_ids: list[str] = []
        done = threading.Event()
        lock = threading.Lock()
        for index in range(task_count):
            self._create_execution(task_id=f"{task_prefix}{index}", queue_name=queue_name)

        def on_claim(_task: RayTaskExecution) -> None:
            with lock:
                claimed_task_ids.append(str(_task.task_id))
                claimed_at.append(time.monotonic())
                if len(claimed_at) >= task_count:
                    done.set()

        group = self._start_workers(
            phase="throughput",
            policy_name=policy_name,
            workers=workers,
            queue_name=queue_name,
            on_claim=on_claim,
            base_interval=base_interval,
            max_interval=max_interval,
            jitter_ratio=jitter_ratio,
            barrier_timeout=barrier_timeout,
            seed=seed,
        )
        try:
            started_at = time.monotonic()
            self._release_workers(group, barrier_timeout=barrier_timeout)
            timeout = max(10.0, task_count * base_interval * 2)
            if not done.wait(timeout):
                self._raise_worker_error(group, policy_name)
                raise CommandError(
                    f"{policy_name} throughput phase claimed {len(claimed_at)}/{task_count} "
                    f"tasks before the {timeout:g}s timeout"
                )
            self._raise_worker_error(group, policy_name)
            self._assert_claim_integrity(
                task_prefix=task_prefix,
                claimed_task_ids=claimed_task_ids,
                worker_ids={
                    identity.worker_id
                    for identity in group.lease_identities
                    if identity is not None
                },
                expected_count=task_count,
                phase=f"{policy_name} throughput",
            )
            return task_count / max(1e-9, max(claimed_at) - started_at)
        finally:
            self._stop_workers(group, max_interval=max_interval)
            self._cleanup_phase_rows(
                task_prefix=task_prefix,
                lease_identities=group.lease_identities,
            )

    def _start_workers(
        self,
        *,
        phase: str,
        policy_name: str,
        workers: int,
        queue_name: str,
        on_claim: Any,
        base_interval: float,
        max_interval: float,
        jitter_ratio: float,
        barrier_timeout: float,
        seed: int,
    ) -> _WorkerGroup:
        stop = threading.Event()
        ready = threading.Barrier(workers + 1)
        metrics = _ThreadMetrics(
            sql_query_times_by_worker=[[] for _ in range(workers)],
            claim_query_times_by_worker=[[] for _ in range(workers)],
            errors=[],
            lock=threading.Lock(),
        )
        worker_id_prefix = f"benchmark-{queue_name}-"
        lease_identities: list[WorkerLeaseIdentity | None] = [None] * workers

        def poll(worker_index: int) -> None:
            close_old_connections()
            try:
                command = WorkerCommand()
                command.stdout = StringIO()
                command._set_worker_id(f"{worker_id_prefix}{worker_index}")
                command.execution_mode = "local"
                command.shutdown_requested = False
                command.active_tasks = {}
                command.ray_core_runner = None
                command.process_task = on_claim  # type: ignore[method-assign]
                command._create_lease(queue_name)
                if command.lease_identity is None:
                    raise RuntimeError("benchmark worker did not acquire a lease identity")
                lease_identities[worker_index] = command.lease_identity
                policy = AdaptivePollingPolicy(
                    base_interval_seconds=base_interval,
                    max_interval_seconds=max_interval,
                    jitter_ratio=jitter_ratio,
                    random_value=random.Random(
                        f"{seed}:{policy_name}:{phase}:{worker_index}"
                    ).random,
                )

                def observe(execute, sql, params, many, context):
                    observed_at = time.monotonic()
                    with metrics.lock:
                        metrics.sql_query_times_by_worker[worker_index].append(observed_at)
                        if _is_claim_query(sql):
                            metrics.claim_query_times_by_worker[worker_index].append(observed_at)
                    return execute(sql, params, many, context)

                with connection.execute_wrapper(observe):
                    ready.wait(timeout=barrier_timeout)
                    next_claim_at = time.monotonic()
                    next_lease_heartbeat_at = next_claim_at
                    while not stop.is_set():
                        now = time.monotonic()
                        if now >= next_lease_heartbeat_at:
                            if not command._update_lease_heartbeat():
                                raise RuntimeError("benchmark worker lease ownership was lost")
                            next_lease_heartbeat_at = now + get_heartbeat_interval().total_seconds()
                        if now >= next_claim_at:
                            activity = bool(
                                command.claim_and_process_tasks([queue_name], concurrency=1)
                            )
                            if command.shutdown_requested:
                                raise RuntimeError("benchmark worker lease ownership was lost")
                            next_claim_at = now + policy.next_delay(activity=activity)
                        stop.wait(
                            max(
                                0.0,
                                min(next_claim_at, next_lease_heartbeat_at) - time.monotonic(),
                            )
                        )
            except threading.BrokenBarrierError:
                if not stop.is_set():
                    with metrics.lock:
                        metrics.errors.append(f"worker {worker_index}: startup barrier broke")
                    stop.set()
            except Exception as exc:
                with metrics.lock:
                    metrics.errors.append(
                        f"worker {worker_index}: {render_console_diagnostic(exc)}"
                    )
                stop.set()
                try:
                    ready.abort()
                except threading.BrokenBarrierError:
                    pass
            finally:
                close_old_connections()

        threads = [
            threading.Thread(
                target=poll,
                args=(index,),
                name=f"poll-benchmark-{phase}-{index}",
                daemon=True,
            )
            for index in range(workers)
        ]
        started_threads: list[threading.Thread] = []
        try:
            for thread in threads:
                thread.start()
                started_threads.append(thread)
        except Exception as exc:
            stop.set()
            ready.abort()
            deadline = time.monotonic() + barrier_timeout
            for thread in started_threads:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            self._delete_exact_leases(lease_identities)
            diagnostic = render_console_diagnostic(exc)
            raise CommandError(f"could not start benchmark workers: {diagnostic}") from None
        return _WorkerGroup(
            stop=stop,
            ready=ready,
            threads=threads,
            metrics=metrics,
            lease_identities=lease_identities,
        )

    @staticmethod
    def _release_workers(group: _WorkerGroup, *, barrier_timeout: float) -> None:
        try:
            group.ready.wait(timeout=barrier_timeout)
        except threading.BrokenBarrierError as exc:
            group.stop.set()
            raise CommandError("benchmark workers did not reach the startup barrier") from exc

    @staticmethod
    def _raise_worker_error(group: _WorkerGroup, policy_name: str) -> None:
        with group.metrics.lock:
            if group.metrics.errors:
                raise CommandError(
                    f"{policy_name} benchmark worker failed: {group.metrics.errors[0]}"
                )

    @staticmethod
    def _stop_workers(group: _WorkerGroup, *, max_interval: float) -> None:
        group.stop.set()
        try:
            group.ready.abort()
        except threading.BrokenBarrierError:
            pass
        join_timeout = max(5.0, max_interval * 2)
        deadline = time.monotonic() + join_timeout
        for thread in group.threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = [thread.name for thread in group.threads if thread.is_alive()]
        if alive:
            raise CommandError(f"benchmark workers did not stop cleanly: {', '.join(alive)}")

    @staticmethod
    def _cleanup_phase_rows(
        *,
        task_prefix: str,
        lease_identities: list[WorkerLeaseIdentity | None],
    ) -> None:
        """Delete only this completed benchmark phase's tasks and leases."""
        RayTaskExecution.objects.filter(task_id__startswith=task_prefix).delete()
        Command._delete_exact_leases(lease_identities)

    @staticmethod
    def _delete_exact_leases(
        lease_identities: list[WorkerLeaseIdentity | None],
    ) -> None:
        """Delete only lease rows acquired by this benchmark phase."""
        for identity in lease_identities:
            if identity is not None:
                TaskWorkerLease.objects.filter(**identity.database_filters()).delete()

    @staticmethod
    def _create_execution(*, task_id: str, queue_name: str) -> None:
        RayTaskExecution.objects.create(
            task_id=task_id,
            callable_path="django_ray.benchmarks.polling_probe",
            queue_name=queue_name,
            state=TaskState.QUEUED,
            args_json="[]",
            kwargs_json="{}",
        )

    @staticmethod
    def _assert_claim_integrity(
        *,
        task_prefix: str,
        claimed_task_ids: list[str],
        worker_ids: set[str],
        expected_count: int,
        phase: str,
    ) -> None:
        unique_claims = set(claimed_task_ids)
        rows = RayTaskExecution.objects.filter(task_id__startswith=task_prefix)
        row_count = rows.count()
        running_count = rows.filter(state=TaskState.RUNNING).count()
        owned_count = rows.filter(claimed_by_worker__in=worker_ids).count()
        if (
            len(claimed_task_ids) != expected_count
            or len(unique_claims) != expected_count
            or row_count != expected_count
            or running_count != expected_count
            or owned_count != expected_count
        ):
            raise CommandError(
                f"{phase} phase claim integrity failed: callbacks={len(claimed_task_ids)}, "
                f"unique={len(unique_claims)}, rows={row_count}, running={running_count}, "
                f"owned={owned_count}, expected={expected_count}"
            )
