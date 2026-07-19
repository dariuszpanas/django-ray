"""Unit tests for the PostgreSQL polling benchmark command."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from django.core.management.base import CommandError, CommandParser

import django_ray.management.commands.django_ray_benchmark_polling as benchmark
from django_ray.management.commands.django_ray_benchmark_polling import (
    BenchmarkResult,
    Command,
    _cross_worker_overlap_metrics,
    _is_claim_query,
    _percentile,
    _ThreadMetrics,
    _WorkerGroup,
)


def _options(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workers": 2,
        "tasks": 5,
        "idle_seconds": 0.2,
        "enqueue_interval_seconds": 0.01,
        "base_interval_seconds": 0.1,
        "max_interval_seconds": 0.5,
        "overlap_window_ms": 25.0,
        "seed": 53,
        "barrier_timeout_seconds": 1.0,
        "json_output": False,
    }
    values.update(overrides)
    return values


def _result(policy: str) -> BenchmarkResult:
    return BenchmarkResult(
        policy=policy,
        workers=2,
        tasks_per_phase=5,
        base_interval_seconds=0.1,
        max_interval_seconds=0.5 if policy == "adaptive" else 0.1,
        idle_claim_queries_per_worker_second=4.0,
        idle_total_sql_per_worker_second=4.0,
        idle_peak_overlapping_workers=2,
        idle_cross_worker_overlap_ratio=0.25,
        claim_latency_p50_ms=50.0,
        claim_latency_p95_ms=100.0,
        burst_claim_throughput_per_second=20.0,
    )


def _group(
    *,
    metrics: _ThreadMetrics | None = None,
    threads: list[threading.Thread] | None = None,
) -> _WorkerGroup:
    return _WorkerGroup(
        stop=threading.Event(),
        ready=threading.Barrier(1),
        threads=threads or [],
        metrics=metrics
        or _ThreadMetrics(
            sql_query_times_by_worker=[[1.0, 1.1, 1.2], [1.01, 1.2, 1.3, 1.4]],
            claim_query_times_by_worker=[[1.0, 1.1], [1.01, 1.2]],
            errors=[],
            lock=threading.Lock(),
        ),
    )


def test_percentile_interpolates_small_samples() -> None:
    assert _percentile([0.1], 0.95) == 0.1
    assert _percentile([0.1, 0.2, 0.3], 0.5) == 0.2
    assert _percentile([0.1, 0.2, 0.3], 0.95) == pytest.approx(0.29)


def test_overlap_metrics_ignore_each_workers_initial_query() -> None:
    assert _cross_worker_overlap_metrics([[], []], window_seconds=0.05) == (0, 0.0)
    assert _cross_worker_overlap_metrics([[0.0], [0.0]], window_seconds=0.05) == (0, 0.0)


def test_overlap_metrics_require_distinct_workers() -> None:
    assert _cross_worker_overlap_metrics(
        [[0.0, 0.10, 0.11, 0.12]],
        window_seconds=0.05,
    ) == (0, 0.0)


def test_overlap_metrics_use_sliding_window_across_fixed_boundaries() -> None:
    peak, ratio = _cross_worker_overlap_metrics(
        [[0.0, 0.001, 0.049], [0.0, 0.051]],
        window_seconds=0.005,
    )
    assert peak == 2
    assert ratio == pytest.approx(2 / 3)


def test_claim_query_detection_matches_production_select_only() -> None:
    table = benchmark.RayTaskExecution._meta.db_table
    assert _is_claim_query(f'SELECT * FROM "{table}" FOR UPDATE SKIP LOCKED') is True
    assert _is_claim_query(f'UPDATE "{table}" SET state = 2') is False
    assert _is_claim_query("SELECT 1 FOR UPDATE") is False


def test_add_arguments_exposes_repeatable_phase_controls() -> None:
    parser = CommandParser(prog="django_ray_benchmark_polling")
    Command().add_arguments(parser)
    options = vars(parser.parse_args([]))
    assert options["workers"] == 4
    assert options["tasks"] == 100
    assert options["seed"] == 53
    assert options["max_interval_seconds"] == 0.5
    assert options["overlap_window_ms"] == 25.0


def test_benchmark_rejects_non_postgresql_database() -> None:
    with pytest.raises(CommandError, match="requires PostgreSQL"):
        Command().handle(**_options())


def test_handle_emits_json_with_environment_metadata(monkeypatch) -> None:
    fake_connection = SimpleNamespace(vendor="postgresql", pg_version=170002)
    monkeypatch.setattr(benchmark, "connection", fake_connection)
    monkeypatch.setattr(Command, "_schema_version", lambda: "0008_priority")
    monkeypatch.setattr(
        Command, "_run_policy", lambda _self, **kwargs: _result(kwargs["policy_name"])
    )
    command = Command()
    command.stdout = StringIO()

    command.handle(**_options(json_output=True))

    payload = json.loads(command.stdout.getvalue())
    assert payload["environment"]["database"] == "postgresql"
    assert payload["environment"]["database_server_version"] == "170002"
    assert payload["environment"]["seed"] == 53
    assert [result["policy"] for result in payload["results"]] == ["fixed", "adaptive"]


def test_handle_emits_human_readable_metrics(monkeypatch) -> None:
    monkeypatch.setattr(benchmark, "connection", SimpleNamespace(vendor="postgresql"))
    monkeypatch.setattr(Command, "_schema_version", lambda: "0008_priority")
    monkeypatch.setattr(
        Command, "_run_policy", lambda _self, **kwargs: _result(kwargs["policy_name"])
    )
    command = Command()
    command.stdout = StringIO()

    command.handle(**_options())

    output = command.stdout.getvalue()
    assert "fixed: idle_claim_queries=4.00" in output
    assert "adaptive: idle_claim_queries=4.00" in output
    assert "peak_overlapping_workers=2" in output
    assert "cross_worker_overlap_ratio=0.250" in output
    assert "burst_throughput=20.00 tasks/s" in output


def test_handle_rejects_maximum_below_base(monkeypatch) -> None:
    monkeypatch.setattr(benchmark, "connection", SimpleNamespace(vendor="postgresql"))
    with pytest.raises(CommandError, match="must be at least"):
        Command().handle(**_options(base_interval_seconds=0.5, max_interval_seconds=0.1))


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "2"])
def test_positive_integer_validation(value: object) -> None:
    with pytest.raises(CommandError, match="positive integer"):
        Command._positive_int(value, "--workers")


@pytest.mark.parametrize("value", [True, 1.5, "53"])
def test_integer_validation(value: object) -> None:
    with pytest.raises(CommandError, match="must be an integer"):
        Command._integer(value, "--seed")
    assert Command._integer(0, "--seed") == 0


@pytest.mark.parametrize("value", [True, 0, -1, "0.1", float("nan"), float("inf")])
def test_positive_float_validation(value: object) -> None:
    with pytest.raises(CommandError, match="positive number"):
        Command._positive_float(value, "--idle-seconds")


def test_environment_metadata_handles_unknown_server_version(monkeypatch) -> None:
    monkeypatch.setattr(benchmark, "connection", SimpleNamespace(vendor="postgresql"))
    monkeypatch.setattr(Command, "_schema_version", lambda: "0008_priority")
    metadata = Command._environment_metadata(
        workers=2,
        tasks=5,
        idle_seconds=0.2,
        enqueue_interval=0.01,
        overlap_window=0.025,
        seed=53,
    )
    assert metadata["database_server_version"] == "unknown"
    assert metadata["django_ray_schema_version"] == "0008_priority"
    assert metadata["django_version"]
    assert metadata["python_version"]
    assert metadata["overlap_window_ms"] == 25.0


def test_schema_version_reports_latest_applied_django_ray_migration(monkeypatch) -> None:
    recorder = Mock()
    recorder.applied_migrations.return_value = {
        ("django_ray", "0007_older"),
        ("other_app", "9999_unrelated"),
        ("django_ray", "0008_priority"),
    }
    recorder_type = Mock(return_value=recorder)
    monkeypatch.setattr(benchmark, "MigrationRecorder", recorder_type)

    assert Command._schema_version() == "0008_priority"
    recorder_type.assert_called_once_with(benchmark.connection)


def test_schema_version_reports_unmigrated_database(monkeypatch) -> None:
    recorder = Mock()
    recorder.applied_migrations.return_value = set()
    monkeypatch.setattr(benchmark, "MigrationRecorder", Mock(return_value=recorder))

    assert Command._schema_version() == "unmigrated"


def test_run_policy_combines_independent_phase_metrics(monkeypatch) -> None:
    command = Command()
    metrics = _ThreadMetrics(
        sql_query_times_by_worker=[[0.0, 0.1], [0.0, 0.11]],
        claim_query_times_by_worker=[[0.0, 0.1], [0.0, 0.11]],
        errors=[],
        lock=threading.Lock(),
    )
    monkeypatch.setattr(
        command,
        "_run_idle_and_latency_phase",
        lambda **_kwargs: (metrics, [0.1, 0.2], 2.0),
    )
    monkeypatch.setattr(command, "_run_throughput_phase", lambda **_kwargs: 30.0)

    result = command._run_policy(
        policy_name="adaptive",
        workers=2,
        task_count=2,
        idle_seconds=1.0,
        enqueue_interval=0.1,
        base_interval=0.1,
        max_interval=0.5,
        jitter_ratio=0.2,
        overlap_window=0.05,
        barrier_timeout=1.0,
        seed=53,
    )

    assert result.idle_claim_queries_per_worker_second == 1.0
    assert result.idle_total_sql_per_worker_second == 1.0
    assert result.claim_latency_p50_ms == pytest.approx(150.0)
    assert result.burst_claim_throughput_per_second == 30.0


def test_idle_latency_phase_snapshots_idle_sql_and_claims_spaced_tasks(monkeypatch) -> None:
    command = Command()
    group = _group()
    callbacks: dict[str, object] = {}
    deleted: list[bool] = []
    observed_times: list[float] = []
    monotonic_values = iter([1.0, 2.0, 3.0, 3.05, 4.0, 4.05, 5.0, 5.05])

    def monotonic() -> float:
        value = next(monotonic_values)
        observed_times.append(value)
        return value

    monkeypatch.setattr(
        command,
        "_start_workers",
        lambda **kwargs: callbacks.update(on_claim=kwargs["on_claim"]) or group,
    )

    def release(*_args, **_kwargs) -> None:
        assert observed_times == [1.0]

    monkeypatch.setattr(command, "_release_workers", release)
    monkeypatch.setattr(command, "_stop_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(command, "_assert_claim_integrity", lambda **_kwargs: None)
    monkeypatch.setattr(benchmark.time, "monotonic", monotonic)
    monkeypatch.setattr(benchmark.time, "sleep", lambda _seconds: None)

    def create(*, task_id: str, queue_name: str) -> None:
        assert queue_name.startswith("django-ray-poll-latency-")
        on_claim = cast(Callable[[object], None], callbacks["on_claim"])
        on_claim(SimpleNamespace(task_id=task_id))

    monkeypatch.setattr(command, "_create_execution", create)
    fake_queryset = SimpleNamespace(delete=lambda: deleted.append(True))
    monkeypatch.setattr(
        benchmark.RayTaskExecution.objects, "filter", lambda **_kwargs: fake_queryset
    )

    idle_metrics, latencies, idle_elapsed = command._run_idle_and_latency_phase(
        policy_name="adaptive",
        workers=2,
        task_count=3,
        idle_seconds=0.001,
        enqueue_interval=0.001,
        base_interval=0.01,
        max_interval=0.05,
        jitter_ratio=0.2,
        barrier_timeout=1.0,
        seed=53,
    )

    assert [len(times) for times in idle_metrics.sql_query_times_by_worker] == [3, 4]
    assert len(latencies) == 3
    assert latencies == pytest.approx([0.05, 0.05, 0.05])
    assert idle_elapsed == 1.0
    assert deleted == [True]


def test_idle_latency_phase_reports_worker_error_and_timeout(monkeypatch) -> None:
    command = Command()
    group = _group(
        metrics=_ThreadMetrics(
            sql_query_times_by_worker=[[]],
            claim_query_times_by_worker=[[]],
            errors=["worker failed"],
            lock=threading.Lock(),
        )
    )
    group.stop.set()
    monkeypatch.setattr(command, "_start_workers", lambda **_kwargs: group)
    monkeypatch.setattr(command, "_release_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(command, "_stop_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(command, "_assert_claim_integrity", lambda **_kwargs: None)
    monkeypatch.setattr(benchmark.RayTaskExecution.objects, "filter", lambda **_kwargs: Mock())

    with pytest.raises(CommandError, match="worker failed"):
        command._run_idle_and_latency_phase(
            policy_name="adaptive",
            workers=1,
            task_count=1,
            idle_seconds=0.001,
            enqueue_interval=0.001,
            base_interval=0.01,
            max_interval=0.05,
            jitter_ratio=0.2,
            barrier_timeout=1.0,
            seed=53,
        )


def test_idle_latency_phase_reports_claim_timeout(monkeypatch) -> None:
    command = Command()
    group = _group(
        metrics=_ThreadMetrics(
            sql_query_times_by_worker=[[]],
            claim_query_times_by_worker=[[]],
            errors=[],
            lock=threading.Lock(),
        )
    )
    monkeypatch.setattr(command, "_start_workers", lambda **_kwargs: group)
    monkeypatch.setattr(command, "_release_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(command, "_stop_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(command, "_create_execution", lambda **_kwargs: None)
    monkeypatch.setattr(benchmark.RayTaskExecution.objects, "filter", lambda **_kwargs: Mock())
    monkeypatch.setattr(threading.Event, "wait", lambda _self, _timeout=None: False)

    with pytest.raises(CommandError, match="latency phase claimed 0/1"):
        command._run_idle_and_latency_phase(
            policy_name="adaptive",
            workers=1,
            task_count=1,
            idle_seconds=0.001,
            enqueue_interval=0.001,
            base_interval=0.01,
            max_interval=0.05,
            jitter_ratio=0.2,
            barrier_timeout=1.0,
            seed=53,
        )


def test_throughput_phase_measures_preloaded_burst(monkeypatch) -> None:
    command = Command()
    group = _group()
    created: list[str] = []
    deleted: list[bool] = []
    monkeypatch.setattr(
        command,
        "_create_execution",
        lambda *, task_id, queue_name: created.append(f"{queue_name}:{task_id}"),
    )

    def start(**kwargs):
        for index in range(3):
            kwargs["on_claim"](SimpleNamespace(task_id=f"task-{index}"))
        return group

    monkeypatch.setattr(command, "_start_workers", start)
    monkeypatch.setattr(command, "_release_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(command, "_stop_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(command, "_assert_claim_integrity", lambda **_kwargs: None)
    fake_queryset = SimpleNamespace(delete=lambda: deleted.append(True))
    monkeypatch.setattr(
        benchmark.RayTaskExecution.objects, "filter", lambda **_kwargs: fake_queryset
    )

    throughput = command._run_throughput_phase(
        policy_name="fixed",
        workers=2,
        task_count=3,
        base_interval=0.01,
        max_interval=0.01,
        jitter_ratio=0.0,
        barrier_timeout=1.0,
        seed=53,
    )

    assert throughput > 0
    assert len(created) == 3
    assert deleted == [True]


def test_throughput_phase_reports_timeout(monkeypatch) -> None:
    command = Command()
    group = _group()
    monkeypatch.setattr(command, "_create_execution", lambda **_kwargs: None)
    monkeypatch.setattr(command, "_start_workers", lambda **_kwargs: group)
    monkeypatch.setattr(command, "_release_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(command, "_stop_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(command, "_assert_claim_integrity", lambda **_kwargs: None)
    monkeypatch.setattr(benchmark.RayTaskExecution.objects, "filter", lambda **_kwargs: Mock())
    monkeypatch.setattr(threading.Event, "wait", lambda _self, _timeout=None: False)

    with pytest.raises(CommandError, match="throughput phase claimed 0/1"):
        command._run_throughput_phase(
            policy_name="fixed",
            workers=1,
            task_count=1,
            base_interval=0.01,
            max_interval=0.01,
            jitter_ratio=0.0,
            barrier_timeout=1.0,
            seed=53,
        )


def test_start_workers_runs_production_claim_under_sql_wrapper(monkeypatch) -> None:
    command = Command()
    claimed = threading.Event()

    class FakeConnection:
        observer: Callable[..., object] | None = None

        @contextmanager
        def execute_wrapper(self, observer):
            self.observer = observer
            yield

    fake_connection = FakeConnection()

    class FakeWorker:
        def __init__(self) -> None:
            self.process_task = None

        def claim_and_process_tasks(self, _queues, concurrency):
            assert concurrency == 1
            table = benchmark.RayTaskExecution._meta.db_table
            observer = cast(Callable[..., object], fake_connection.observer)
            observer(
                lambda _sql, _params, _many, _context: None,
                f'SELECT * FROM "{table}" FOR UPDATE SKIP LOCKED',
                (),
                False,
                {},
            )
            process_task = cast(Callable[[object], None], self.process_task)
            process_task(SimpleNamespace(task_id="claimed"))
            return 1

    monkeypatch.setattr(benchmark, "connection", fake_connection)
    monkeypatch.setattr(benchmark, "WorkerCommand", FakeWorker)
    monkeypatch.setattr(benchmark, "close_old_connections", lambda: None)
    group = command._start_workers(
        phase="latency",
        policy_name="adaptive",
        workers=1,
        queue_name="queue",
        on_claim=lambda _task: claimed.set(),
        base_interval=0.01,
        max_interval=0.05,
        jitter_ratio=0.2,
        barrier_timeout=1.0,
        seed=53,
    )
    command._release_workers(group, barrier_timeout=1.0)
    assert claimed.wait(timeout=1.0)
    command._stop_workers(group, max_interval=0.05)
    assert group.metrics.sql_query_times_by_worker[0]
    assert group.metrics.claim_query_times_by_worker[0]


def test_start_workers_reports_constructor_and_thread_start_failures(monkeypatch) -> None:
    command = Command()

    class BrokenWorker:
        def __init__(self) -> None:
            raise RuntimeError("worker setup failed")

    monkeypatch.setattr(benchmark, "WorkerCommand", BrokenWorker)
    monkeypatch.setattr(benchmark, "close_old_connections", lambda: None)
    group = command._start_workers(
        phase="latency",
        policy_name="adaptive",
        workers=1,
        queue_name="queue",
        on_claim=lambda _task: None,
        base_interval=0.01,
        max_interval=0.05,
        jitter_ratio=0.2,
        barrier_timeout=1.0,
        seed=53,
    )
    with pytest.raises(CommandError, match="startup barrier"):
        command._release_workers(group, barrier_timeout=1.0)
    with pytest.raises(CommandError, match="worker setup failed"):
        command._raise_worker_error(group, "adaptive")
    command._stop_workers(group, max_interval=0.05)

    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("no thread")))
    with pytest.raises(CommandError, match="could not start benchmark workers"):
        command._start_workers(
            phase="latency",
            policy_name="adaptive",
            workers=1,
            queue_name="queue",
            on_claim=lambda _task: None,
            base_interval=0.01,
            max_interval=0.05,
            jitter_ratio=0.2,
            barrier_timeout=1.0,
            seed=53,
        )


def test_worker_startup_barrier_and_abort_errors_are_contained(monkeypatch) -> None:
    command = Command()

    class BrokenBarrier:
        def __init__(self, _parties: int) -> None:
            pass

        def wait(self, timeout=None) -> None:
            del timeout
            raise threading.BrokenBarrierError

        def abort(self) -> None:
            raise threading.BrokenBarrierError

    monkeypatch.setattr(benchmark.threading, "Barrier", BrokenBarrier)
    monkeypatch.setattr(benchmark, "close_old_connections", lambda: None)
    group = command._start_workers(
        phase="latency",
        policy_name="adaptive",
        workers=1,
        queue_name="queue",
        on_claim=lambda _task: None,
        base_interval=0.01,
        max_interval=0.05,
        jitter_ratio=0.2,
        barrier_timeout=1.0,
        seed=53,
    )
    group.threads[0].join(timeout=1.0)

    assert group.metrics.errors == ["worker 0: startup barrier broke"]
    command._stop_workers(group, max_interval=0.05)

    class BrokenWorker:
        def __init__(self) -> None:
            raise RuntimeError("worker construction failed")

    monkeypatch.setattr(benchmark, "WorkerCommand", BrokenWorker)
    exception_group = command._start_workers(
        phase="latency",
        policy_name="adaptive",
        workers=1,
        queue_name="queue",
        on_claim=lambda _task: None,
        base_interval=0.01,
        max_interval=0.05,
        jitter_ratio=0.2,
        barrier_timeout=1.0,
        seed=53,
    )
    exception_group.threads[0].join(timeout=1.0)

    assert exception_group.metrics.errors == ["worker 0: RuntimeError: worker construction failed"]
    command._stop_workers(exception_group, max_interval=0.05)


def test_partial_thread_start_failure_joins_started_threads(monkeypatch) -> None:
    command = Command()
    joined: list[str] = []

    class FakeThread:
        instances = 0

        def __init__(self, *, target, args, name, daemon) -> None:
            del target, args, daemon
            self.name = name
            self.index = FakeThread.instances
            FakeThread.instances += 1

        def start(self) -> None:
            if self.index == 1:
                raise RuntimeError("second thread failed")

        def join(self, timeout=None) -> None:
            del timeout
            joined.append(self.name)

    monkeypatch.setattr(benchmark.threading, "Thread", FakeThread)

    with pytest.raises(CommandError, match="second thread failed"):
        command._start_workers(
            phase="latency",
            policy_name="adaptive",
            workers=2,
            queue_name="queue",
            on_claim=lambda _task: None,
            base_interval=0.01,
            max_interval=0.05,
            jitter_ratio=0.2,
            barrier_timeout=1.0,
            seed=53,
        )

    assert joined == ["poll-benchmark-latency-0"]


def test_stop_workers_rejects_lingering_thread() -> None:
    lingering = SimpleNamespace(
        name="lingering",
        join=Mock(),
        is_alive=lambda: True,
    )
    group = _group(threads=[lingering])  # type: ignore[list-item]
    with pytest.raises(CommandError, match="did not stop cleanly"):
        Command._stop_workers(group, max_interval=0.01)


def test_create_execution_uses_realistic_payload(monkeypatch) -> None:
    create = Mock()
    monkeypatch.setattr(benchmark.RayTaskExecution.objects, "create", create)

    Command._create_execution(task_id="task", queue_name="queue")

    create.assert_called_once_with(
        task_id="task",
        callable_path="django_ray.benchmarks.polling_probe",
        queue_name="queue",
        state=benchmark.TaskState.QUEUED,
        args_json="[]",
        kwargs_json="{}",
    )


def test_claim_integrity_reports_exact_counts(monkeypatch) -> None:
    rows = Mock()
    rows.count.return_value = 2
    rows.filter.side_effect = [
        SimpleNamespace(count=lambda: 1),
        SimpleNamespace(count=lambda: 2),
    ]
    monkeypatch.setattr(benchmark.RayTaskExecution.objects, "filter", Mock(return_value=rows))

    with pytest.raises(CommandError, match="callbacks=2, unique=1, rows=2, running=1"):
        Command._assert_claim_integrity(
            task_prefix="poll-",
            claimed_task_ids=["duplicate", "duplicate"],
            expected_count=2,
            phase="fixed latency",
        )
