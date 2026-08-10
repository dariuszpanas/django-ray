"""Read-only execution-protocol status contracts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import is_dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from threading import Event

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext

from django_ray import protocol_status as protocol_status_module
from django_ray.management.commands import (
    django_ray_protocol_status as protocol_status_command,
)
from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskExecutionProtocolPolicy,
    TaskState,
    TaskWorkerLease,
)
from django_ray.protocol_status import (
    PROTOCOL_STATUS_GROUP_LIMIT,
    PROTOCOL_STATUS_OUTPUT_MAX_BYTES,
    PROTOCOL_STATUS_SCHEMA,
    PROTOCOL_STATUS_SCHEMA_VERSION,
    ProtocolCapabilitySection,
    ProtocolLeaseCounts,
    ProtocolPolicyStatus,
    ProtocolStatusBlockerCode,
    ProtocolStatusError,
    ProtocolStatusReport,
    annotate_execution_protocol_availability,
    build_protocol_status,
    protocol_status_to_dict,
    render_protocol_status_json,
    render_protocol_status_text,
)
from django_ray.runner.leasing import get_lease_duration

pytestmark = pytest.mark.django_db(transaction=True)


def _legacy_lease(
    worker_id: str,
    *,
    heartbeat_at: datetime,
    is_active: bool = True,
) -> TaskWorkerLease:
    return TaskWorkerLease.objects.create(
        worker_id=worker_id,
        hostname="legacy-status-host",
        pid=1001,
        last_heartbeat_at=heartbeat_at,
        is_active=is_active,
        stopped_at=None if is_active else heartbeat_at,
    )


def _explicit_lease(
    worker_id: str,
    *,
    minimum: int,
    maximum: int,
    heartbeat_at: datetime,
    is_active: bool = True,
    package_version: str = "0.5.0-test",
    queue: str = "default",
) -> TaskWorkerLease:
    return TaskWorkerLease.objects.create(
        worker_id=worker_id,
        hostname="explicit-status-host",
        pid=1002,
        capability_schema_version=1,
        django_ray_version=package_version,
        queue_name=queue,
        min_supported_execution_protocol_version=minimum,
        max_supported_execution_protocol_version=maximum,
        legacy_admission_token=None,
        last_heartbeat_at=heartbeat_at,
        is_active=is_active,
        stopped_at=None if is_active else heartbeat_at,
    )


def _execution(
    task_id: str,
    *,
    protocol: int,
    queue: str,
    state: TaskState = TaskState.QUEUED,
    metadata_schema: int = 1,
) -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="testproject.tasks.add_numbers",
        metadata_schema_version=metadata_schema,
        execution_protocol_version=protocol,
        queue_name=queue,
        state=state,
    )


def _close_legacy_admission() -> None:
    LegacyWorkerAdmissionToken.objects.get(singleton_key=1).delete()
    TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
        legacy_worker_admission_enabled=False,
        revision=2,
    )


def _database_snapshot() -> dict[str, list[dict[str, object]]]:
    models = (
        TaskExecutionProtocolPolicy,
        LegacyWorkerAdmissionToken,
        TaskWorkerLease,
        RayTaskExecution,
    )
    return {
        model._meta.label_lower: list(model.objects.order_by(model._meta.pk.name).values())
        for model in models
    }


def _assert_read_only(queries: list[dict[str, str]]) -> list[dict[str, str]]:
    assert queries
    data_queries: list[dict[str, str]] = []
    for query in queries:
        sql = " ".join(query["sql"].strip().upper().split())
        if sql in {"BEGIN", "COMMIT", "ROLLBACK"}:
            continue
        if sql.startswith("SET TRANSACTION"):
            assert sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            continue
        assert sql.startswith(("SELECT", "WITH")), sql
        assert "FOR UPDATE" not in sql
        assert "FOR SHARE" not in sql
        assert "LOCK TABLE" not in sql
        assert "PG_ADVISORY" not in sql
        data_queries.append(query)
    return data_queries


def test_protocol_availability_annotation_correlates_ranges_at_one_frozen_cutoff() -> None:
    observed_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    cutoff = observed_at - get_lease_duration()
    _close_legacy_admission()
    _explicit_lease(
        "availability-live",
        minimum=2,
        maximum=3,
        heartbeat_at=cutoff,
        package_version="not-a-semver",
        queue="worker-only-queue",
    )
    _explicit_lease(
        "availability-stale",
        minimum=4,
        maximum=4,
        heartbeat_at=cutoff - timedelta(microseconds=1),
    )
    for protocol in (1, 2, 3, 4):
        _execution(
            f"availability-v{protocol}",
            protocol=protocol,
            queue="task-only-queue",
        )

    with CaptureQueriesContext(connection) as captured:
        rows = dict(
            annotate_execution_protocol_availability(
                RayTaskExecution.objects.order_by("task_id"),
                observed_at=observed_at,
                field_name="_compatible",
            ).values_list("task_id", "_compatible")
        )

    assert len(_assert_read_only(captured.captured_queries)) == 1
    assert rows == {
        "availability-v1": False,
        "availability-v2": True,
        "availability-v3": True,
        "availability-v4": False,
    }
    sql = captured.captured_queries[0]["sql"].lower()
    assert "exists" in sql
    assert "queue_name" not in sql
    assert "not-a-semver" not in sql


def test_protocol_availability_legacy_and_policy_corruption_fail_closed() -> None:
    observed_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    cutoff = observed_at - get_lease_duration()
    _legacy_lease("availability-legacy", heartbeat_at=cutoff)
    _execution("availability-policy-v1", protocol=1, queue="default")
    _execution(
        "availability-policy-v2",
        protocol=2,
        queue="default",
        state=TaskState.SUCCEEDED,
    )

    def availability() -> dict[str, bool]:
        return dict(
            annotate_execution_protocol_availability(
                RayTaskExecution.objects.order_by("task_id"),
                observed_at=observed_at,
            ).values_list("task_id", "protocol_compatible_worker_available")
        )

    assert availability() == {
        "availability-policy-v1": True,
        "availability-policy-v2": False,
    }

    TaskWorkerLease.objects.all().delete()
    LegacyWorkerAdmissionToken.objects.get(singleton_key=1).delete()
    _explicit_lease(
        "availability-explicit",
        minimum=1,
        maximum=2,
        heartbeat_at=cutoff,
    )
    assert set(availability().values()) == {False}

    TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
        legacy_worker_admission_enabled=False,
        revision=2,
    )
    assert set(availability().values()) == {True}

    TaskExecutionProtocolPolicy.objects.get(singleton_key=1).delete()
    assert set(availability().values()) == {False}


def test_protocol_availability_fails_closed_for_any_malformed_lease_shape() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("SQLite corruption probe uses ignore_check_constraints")
    observed_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    _close_legacy_admission()
    _explicit_lease(
        "availability-valid",
        minimum=2,
        maximum=2,
        heartbeat_at=observed_at,
    )
    execution = _execution("availability-malformed-v2", protocol=2, queue="default")

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA ignore_check_constraints = ON")
    try:
        malformed = TaskWorkerLease.objects.create(
            worker_id="availability-malformed",
            hostname="malformed-host",
            pid=1003,
            capability_schema_version=1,
            django_ray_version="0.5.0-test",
            min_supported_execution_protocol_version=2,
            max_supported_execution_protocol_version=1,
            legacy_admission_token=None,
            last_heartbeat_at=observed_at,
        )
    finally:
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA ignore_check_constraints = OFF")

    annotated = annotate_execution_protocol_availability(
        RayTaskExecution.objects.filter(pk=execution.pk),
        observed_at=observed_at,
    ).get()
    assert annotated.protocol_compatible_worker_available is False

    TaskWorkerLease.objects.filter(pk=malformed.pk).delete()
    annotated = annotate_execution_protocol_availability(
        RayTaskExecution.objects.filter(pk=execution.pk),
        observed_at=observed_at,
    ).get()
    assert annotated.protocol_compatible_worker_available is True


def _seed_open_status(observed_at: datetime) -> None:
    cutoff = observed_at - get_lease_duration()
    _legacy_lease("status-legacy-live", heartbeat_at=cutoff)
    _legacy_lease(
        "status-legacy-stale",
        heartbeat_at=cutoff - timedelta(microseconds=1),
    )
    _explicit_lease(
        "status-explicit-live",
        minimum=2,
        maximum=3,
        heartbeat_at=cutoff,
        package_version="not-semver-sensitive-version",
        queue="a-different-informational-queue",
    )
    _explicit_lease(
        "status-explicit-stale",
        minimum=4,
        maximum=4,
        heartbeat_at=cutoff - timedelta(microseconds=1),
    )
    _explicit_lease(
        "status-explicit-inactive",
        minimum=5,
        maximum=5,
        heartbeat_at=observed_at,
        is_active=False,
    )

    _execution("status-v1-a", protocol=1, queue="zeta")
    _execution(
        "status-v1-legacy-metadata",
        protocol=1,
        queue="zeta",
        metadata_schema=0,
    )
    _execution(
        "status-terminal-v99",
        protocol=99,
        queue="terminal-control",
        state=TaskState.SUCCEEDED,
    )


def _seed_closed_status(observed_at: datetime) -> None:
    LegacyWorkerAdmissionToken.objects.get(singleton_key=1).delete()
    TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
        legacy_worker_admission_enabled=False,
        revision=2,
    )
    cutoff = observed_at - get_lease_duration()
    _explicit_lease(
        "status-explicit-live",
        minimum=2,
        maximum=3,
        heartbeat_at=cutoff,
        package_version="not-semver-sensitive-version",
        queue="a-different-informational-queue",
    )
    _explicit_lease(
        "status-explicit-stale",
        minimum=4,
        maximum=4,
        heartbeat_at=cutoff - timedelta(microseconds=1),
    )
    _explicit_lease(
        "status-explicit-inactive",
        minimum=5,
        maximum=5,
        heartbeat_at=observed_at,
        is_active=False,
    )

    _execution("status-v1", protocol=1, queue="zeta")
    _execution(
        "status-v2",
        protocol=2,
        queue="alpha",
        state=TaskState.RUNNING,
    )
    _execution(
        "status-v4",
        protocol=4,
        queue="beta",
        state=TaskState.CANCELLING,
    )
    _execution("status-v9", protocol=9, queue="gamma")
    _execution(
        "status-terminal-v99",
        protocol=99,
        queue="terminal-control",
        state=TaskState.SUCCEEDED,
    )


def _build_read_only_status(observed_at: datetime) -> ProtocolStatusReport:
    before = _database_snapshot()
    with CaptureQueriesContext(connection) as captured:
        report = build_protocol_status(using="default", observed_at=observed_at)
    assert _database_snapshot() == before
    assert len(_assert_read_only(captured.captured_queries)) == 14
    return report


def _assert_protocol_status_is_versioned_bounded_and_read_only() -> None:
    observed_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    _seed_open_status(observed_at)
    open_report = _build_read_only_status(observed_at)

    assert open_report.leases.total == 5
    assert open_report.leases.active == 4
    assert open_report.leases.heartbeat_live == 2
    assert open_report.leases.stale_active == 2
    assert open_report.leases.inactive == 1
    assert open_report.leases.active_legacy == 2
    assert open_report.leases.heartbeat_live_legacy == 1
    assert open_report.leases.stale_active_legacy == 1
    assert open_report.leases.active_explicit == 2
    assert open_report.leases.heartbeat_live_explicit == 1
    assert open_report.leases.stale_active_explicit == 1
    assert [
        (group.kind, group.minimum, group.maximum, group.heartbeat_live_leases)
        for group in open_report.capabilities.groups
    ] == [
        ("explicit", 2, 3, 1),
        ("legacy", 1, 1, 1),
    ]
    assert open_report.nonterminal_work.total_tasks == 2
    assert open_report.unsupported_work.total_tasks == 0
    assert open_report.legacy_metadata_nonterminal_count == 1
    assert open_report.non_v1_nonterminal_count == 0
    assert open_report.no_upgraded_reader_nonterminal_count == 2
    open_blockers = {blocker.code: blocker.count for blocker in open_report.blockers}
    assert open_blockers[ProtocolStatusBlockerCode.ACTIVE_LEGACY_LEASES] == 2
    assert open_blockers[ProtocolStatusBlockerCode.ACTIVE_UPGRADED_LEASES] == 2
    assert open_blockers[ProtocolStatusBlockerCode.LEGACY_METADATA_PROVENANCE_UNATTESTED] == 1
    assert open_blockers[ProtocolStatusBlockerCode.NO_UPGRADED_READER_CAPACITY] == 2
    assert ProtocolStatusBlockerCode.UNSUPPORTED_NONTERMINAL_WORK not in open_blockers

    RayTaskExecution.objects.all().delete()
    TaskWorkerLease.objects.all().delete()
    _seed_closed_status(observed_at)
    report = _build_read_only_status(observed_at)
    payload = protocol_status_to_dict(report)
    assert is_dataclass(report)
    assert type(report).__dataclass_params__.frozen is True
    assert report.schema == PROTOCOL_STATUS_SCHEMA
    assert report.schema_version == PROTOCOL_STATUS_SCHEMA_VERSION
    assert report.observed_at == observed_at
    assert report.lease_heartbeat_cutoff == observed_at - get_lease_duration()
    assert payload["schema"] == "django-ray.protocol-status"
    assert payload["schema_version"] == 1
    assert payload["queue_capacity_attested"] is False

    assert report.policy.schema_version == 1
    assert report.policy.active_write_protocol_version == 1
    assert report.policy.legacy_worker_admission_enabled is False
    assert report.policy.legacy_admission_token_present is False
    assert report.policy.revision == 2
    assert report.leases.total == 3
    assert report.leases.active == 2
    assert report.leases.heartbeat_live == 1
    assert report.leases.stale_active == 1
    assert report.leases.inactive == 1
    assert report.leases.active_legacy == 0
    assert report.leases.heartbeat_live_legacy == 0
    assert report.leases.stale_active_legacy == 0
    assert report.leases.active_explicit == 2
    assert report.leases.heartbeat_live_explicit == 1
    assert report.leases.stale_active_explicit == 1

    assert [
        (group.kind, group.minimum, group.maximum, group.heartbeat_live_leases)
        for group in report.capabilities.groups
    ] == [
        ("explicit", 2, 3, 1),
    ]
    assert report.capabilities.total_groups == 1
    assert report.capabilities.total_leases == 1
    assert report.capabilities.omitted_groups == 0
    assert report.capabilities.omitted_leases == 0

    assert [
        (
            group.queue,
            group.state,
            group.execution_protocol_version,
            group.count,
        )
        for group in report.nonterminal_work.groups
    ] == [
        ("alpha", TaskState.RUNNING, 2, 1),
        ("beta", TaskState.CANCELLING, 4, 1),
        ("gamma", TaskState.QUEUED, 9, 1),
        ("zeta", TaskState.QUEUED, 1, 1),
    ]
    assert report.nonterminal_work.total_groups == 4
    assert report.nonterminal_work.total_tasks == 4
    assert [
        (
            group.queue,
            group.state,
            group.execution_protocol_version,
            group.count,
        )
        for group in report.unsupported_work.groups
    ] == [
        ("beta", TaskState.CANCELLING, 4, 1),
        ("gamma", TaskState.QUEUED, 9, 1),
        ("zeta", TaskState.QUEUED, 1, 1),
    ]
    assert report.unsupported_work.total_groups == 3
    assert report.unsupported_work.total_tasks == 3
    assert report.legacy_metadata_nonterminal_count == 0
    assert report.non_v1_nonterminal_count == 3
    assert report.no_upgraded_reader_nonterminal_count == 3

    blockers = {blocker.code: (blocker.scope, blocker.count) for blocker in report.blockers}
    assert blockers == {
        ProtocolStatusBlockerCode.ACTIVE_UPGRADED_LEASES: ("code_rollback", 2),
        ProtocolStatusBlockerCode.LEGACY_PRODUCERS_UNATTESTED: ("legacy_close", None),
        ProtocolStatusBlockerCode.LEGACY_READERS_UNATTESTED: ("code_rollback", None),
        ProtocolStatusBlockerCode.NON_V1_NONTERMINAL_WORK: ("code_rollback", 3),
        ProtocolStatusBlockerCode.NO_UPGRADED_READER_CAPACITY: (
            "reader_retirement",
            3,
        ),
        ProtocolStatusBlockerCode.QUEUE_CAPACITY_UNATTESTED: ("capacity", None),
        ProtocolStatusBlockerCode.RAY_TARGET_READINESS_UNATTESTED: ("capacity", None),
        ProtocolStatusBlockerCode.REMOTE_WORK_RETIREMENT_UNATTESTED: (
            "code_rollback",
            None,
        ),
        ProtocolStatusBlockerCode.UNSUPPORTED_NONTERMINAL_WORK: ("capacity", 3),
    }

    canonical = render_protocol_status_json(report)
    rendered_text = render_protocol_status_text(report)
    assert json.loads(canonical) == payload
    assert len(canonical.encode("utf-8")) < PROTOCOL_STATUS_OUTPUT_MAX_BYTES
    assert len(rendered_text.encode("utf-8")) < PROTOCOL_STATUS_OUTPUT_MAX_BYTES
    for sensitive_value in (
        "status-legacy-live",
        "status-explicit-live",
        "legacy-status-host",
        "explicit-status-host",
        "not-semver-sensitive-version",
        "testproject.tasks.add_numbers",
        "status-v4",
    ):
        assert sensitive_value not in canonical
        assert sensitive_value not in rendered_text


def test_sqlite_protocol_status_is_versioned_bounded_and_read_only() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_protocol_status_is_versioned_bounded_and_read_only()


@pytest.mark.postgresql
def test_postgresql_protocol_status_is_versioned_bounded_and_read_only() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_protocol_status_is_versioned_bounded_and_read_only()


@pytest.mark.postgresql
def test_postgresql_protocol_status_observes_one_repeatable_read_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    observed_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    lease_snapshot_complete = Event()
    writer_committed = Event()
    original_capability_section = protocol_status_module._capability_section

    def pause_after_lease_snapshot(
        *,
        using: str,
        cutoff: datetime,
        policy: ProtocolPolicyStatus,
        leases: ProtocolLeaseCounts,
    ) -> ProtocolCapabilitySection:
        section = original_capability_section(
            using=using,
            cutoff=cutoff,
            policy=policy,
            leases=leases,
        )
        lease_snapshot_complete.set()
        if not writer_committed.wait(timeout=10):
            raise TimeoutError("concurrent status writer did not commit")
        return section

    monkeypatch.setattr(
        protocol_status_module,
        "_capability_section",
        pause_after_lease_snapshot,
    )

    def read_status() -> ProtocolStatusReport:
        close_old_connections()
        try:
            return build_protocol_status(using="default", observed_at=observed_at)
        finally:
            close_old_connections()

    def write_lease_and_task() -> None:
        close_old_connections()
        try:
            with transaction.atomic(using="default"):
                TaskWorkerLease.objects.using("default").create(
                    worker_id="status-concurrent-reader",
                    hostname="concurrent-status-host",
                    pid=4001,
                    capability_schema_version=1,
                    django_ray_version="0.5.0-test",
                    min_supported_execution_protocol_version=1,
                    max_supported_execution_protocol_version=1,
                    legacy_admission_token=None,
                    last_heartbeat_at=observed_at,
                )
                RayTaskExecution.objects.using("default").create(
                    task_id="status-concurrent-task",
                    callable_path="testproject.tasks.add_numbers",
                    execution_protocol_version=1,
                    queue_name="concurrent",
                    state=TaskState.QUEUED,
                )
            writer_committed.set()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(read_status)
        assert lease_snapshot_complete.wait(timeout=10)
        writer = executor.submit(write_lease_and_task)
        writer.result(timeout=20)
        concurrent_report = reader.result(timeout=20)

    assert (
        concurrent_report.leases.total,
        concurrent_report.capabilities.total_leases,
        concurrent_report.nonterminal_work.total_tasks,
    ) == (0, 0, 0)

    monkeypatch.setattr(
        protocol_status_module,
        "_capability_section",
        original_capability_section,
    )
    post_commit_report = build_protocol_status(
        using="default",
        observed_at=observed_at,
    )
    assert (
        post_commit_report.leases.total,
        post_commit_report.capabilities.total_leases,
        post_commit_report.nonterminal_work.total_tasks,
    ) == (1, 1, 1)
    assert post_commit_report.unsupported_work.total_tasks == 0


def test_protocol_status_groups_are_ordered_and_exactly_truncated() -> None:
    observed_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    _close_legacy_admission()
    with CaptureQueriesContext(connection) as empty_queries:
        build_protocol_status(using="default", observed_at=observed_at)
    assert len(_assert_read_only(empty_queries.captured_queries)) == 14

    leases: list[TaskWorkerLease] = []
    executions: list[RayTaskExecution] = []
    for index in range(PROTOCOL_STATUS_GROUP_LIMIT + 2):
        copies = 1 if index < PROTOCOL_STATUS_GROUP_LIMIT else index - 62
        for copy in range(copies):
            leases.append(
                TaskWorkerLease(
                    worker_id=f"status-range-{index:03d}-{copy}",
                    hostname="bounded-range-host",
                    pid=2000 + index,
                    capability_schema_version=1,
                    django_ray_version="0.5.0-test",
                    min_supported_execution_protocol_version=index + 1,
                    max_supported_execution_protocol_version=index + 1,
                    legacy_admission_token=None,
                    last_heartbeat_at=observed_at,
                )
            )
            executions.append(
                RayTaskExecution(
                    task_id=f"status-group-{index:03d}-{copy}",
                    callable_path="testproject.tasks.add_numbers",
                    execution_protocol_version=100 + index,
                    queue_name=f"queue-{index:03d}",
                    state=TaskState.QUEUED,
                )
            )
    TaskWorkerLease.objects.bulk_create(leases)
    RayTaskExecution.objects.bulk_create(executions)
    before = _database_snapshot()

    with CaptureQueriesContext(connection) as captured:
        report = build_protocol_status(using="default", observed_at=observed_at)

    assert _database_snapshot() == before
    assert len(_assert_read_only(captured.captured_queries)) == 14
    assert len(_assert_read_only(empty_queries.captured_queries)) == 14
    bounded_group_queries = [
        query["sql"].upper()
        for query in captured.captured_queries
        if "GROUP BY" in query["sql"].upper()
        and "ORDER BY" in query["sql"].upper()
        and "LIMIT" in query["sql"].upper()
    ]
    assert len(bounded_group_queries) == 3
    assert all("LIMIT 65" in query for query in bounded_group_queries)

    assert len(report.capabilities.groups) == PROTOCOL_STATUS_GROUP_LIMIT
    assert [group.minimum for group in report.capabilities.groups] == list(range(1, 65))
    assert report.capabilities.total_groups == 66
    assert report.capabilities.total_leases == 69
    assert report.capabilities.omitted_groups == 2
    assert report.capabilities.omitted_leases == 5

    for section in (report.nonterminal_work, report.unsupported_work):
        assert len(section.groups) == PROTOCOL_STATUS_GROUP_LIMIT
        assert [group.queue for group in section.groups] == [
            f"queue-{index:03d}" for index in range(64)
        ]
        assert section.total_groups == 66
        assert section.total_tasks == 69
        assert section.omitted_groups == 2
        assert section.omitted_tasks == 5
    assert report.no_upgraded_reader_nonterminal_count == 69


def test_protocol_status_output_budget_preserves_complete_parseable_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert PROTOCOL_STATUS_GROUP_LIMIT == 64
    assert PROTOCOL_STATUS_OUTPUT_MAX_BYTES == 65_536
    observed_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    _close_legacy_admission()
    TaskWorkerLease.objects.bulk_create(
        [
            TaskWorkerLease(
                worker_id=f"status-budget-range-{index:02d}",
                hostname="budget-range-host",
                pid=3000 + index,
                capability_schema_version=1,
                django_ray_version="0.5.0-test",
                min_supported_execution_protocol_version=index + 1,
                max_supported_execution_protocol_version=index + 1,
                legacy_admission_token=None,
                last_heartbeat_at=observed_at,
            )
            for index in range(PROTOCOL_STATUS_GROUP_LIMIT)
        ]
    )

    def budget_queue(index: int) -> str:
        if index == 0:
            return "00\n\r\x1b[31m" + "🙂" * 91
        if index == PROTOCOL_STATUS_GROUP_LIMIT:
            secret_prefix = "zz token=sk-proj-abcdefghijklmnopqrstuvwxyz123456 "
            return secret_prefix + "🙂" * (100 - len(secret_prefix))
        return f"{index:02d}" + "🙂" * 98

    RayTaskExecution.objects.bulk_create(
        [
            RayTaskExecution(
                task_id=f"status-budget-task-{index:02d}",
                callable_path="testproject.tasks.add_numbers",
                execution_protocol_version=1000 + index,
                queue_name=budget_queue(index),
                state=TaskState.QUEUED,
            )
            for index in range(PROTOCOL_STATUS_GROUP_LIMIT + 1)
        ]
    )

    report = build_protocol_status(using="default", observed_at=observed_at)
    repeated = build_protocol_status(using="default", observed_at=observed_at)
    encoded = render_protocol_status_json(report)
    rendered_text = render_protocol_status_text(report)

    assert report == repeated
    assert encoded == render_protocol_status_json(repeated)
    assert rendered_text == render_protocol_status_text(repeated)
    assert json.loads(encoded) == protocol_status_to_dict(report)
    assert len(encoded.encode("utf-8")) < PROTOCOL_STATUS_OUTPUT_MAX_BYTES
    assert len(rendered_text.encode("utf-8")) < PROTOCOL_STATUS_OUTPUT_MAX_BYTES
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz123456" not in encoded
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz123456" not in rendered_text
    assert "\x1b" not in encoded
    assert "\x1b" not in rendered_text
    assert "\r" not in encoded
    assert "\r" not in rendered_text
    sections = (
        report.capabilities,
        report.nonterminal_work,
        report.unsupported_work,
    )
    assert any(section.omitted_groups > 0 for section in sections), (
        len(encoded.encode("utf-8")),
        len(rendered_text.encode("utf-8")),
    )
    assert report.nonterminal_work.total_groups == PROTOCOL_STATUS_GROUP_LIMIT + 1
    assert report.unsupported_work.total_groups == PROTOCOL_STATUS_GROUP_LIMIT + 1
    assert report.nonterminal_work.omitted_groups + report.unsupported_work.omitted_groups > 2
    assert (
        len(report.capabilities.groups) + report.capabilities.omitted_groups
        == report.capabilities.total_groups
    )
    assert (
        sum(group.heartbeat_live_leases for group in report.capabilities.groups)
        + report.capabilities.omitted_leases
        == report.capabilities.total_leases
    )
    assert report.capabilities.omitted_leases == report.capabilities.omitted_groups
    for section in (report.nonterminal_work, report.unsupported_work):
        assert len(section.groups) + section.omitted_groups == section.total_groups
        assert sum(group.count for group in section.groups) + section.omitted_tasks == (
            section.total_tasks
        )
        assert section.omitted_tasks == section.omitted_groups

    monkeypatch.setattr(
        protocol_status_command,
        "build_protocol_status",
        lambda *, using: report,
    )
    for arguments in (("--json",), ()):
        output = StringIO()
        call_command("django_ray_protocol_status", *arguments, stdout=output)
        command_output = output.getvalue()
        assert len(command_output.encode("utf-8")) <= PROTOCOL_STATUS_OUTPUT_MAX_BYTES
        if arguments:
            assert json.loads(command_output) == protocol_status_to_dict(report)


def test_protocol_status_normalizes_and_redacts_queue_text() -> None:
    _close_legacy_admission()
    control_queue = "queue\nname\r\x1b[31m"
    secret_queue = "tenant token=sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    _execution("status-control-queue", protocol=2, queue=control_queue)
    _execution("status-secret-queue", protocol=2, queue=secret_queue)

    report = build_protocol_status(
        using="default",
        observed_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    )

    queues = {group.queue for group in report.nonterminal_work.groups}
    assert queues == {"queue\nname\n", "[REDACTED]"}
    encoded = render_protocol_status_json(report)
    rendered_text = render_protocol_status_text(report)
    assert secret_queue not in encoded
    assert secret_queue not in rendered_text
    assert "[REDACTED]" in encoded
    assert control_queue not in encoded
    assert control_queue not in rendered_text
    assert 'queue="queue\\nname\\n"' in rendered_text


def test_protocol_status_bounds_oversized_sqlite_queue_before_materialization() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("SQLite alone permits overlength VARCHAR values")
    _close_legacy_admission()
    oversized_queue = "q" * 100_000
    _execution("status-oversized-queue", protocol=2, queue=oversized_queue)

    with CaptureQueriesContext(connection) as captured:
        report = build_protocol_status(
            using="default",
            observed_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        )

    data_queries = _assert_read_only(captured.captured_queries)
    assert len(data_queries) == 14
    bounded_queue_queries = [
        query["sql"].upper()
        for query in data_queries
        if "DJANGO_RAY_RAYTASKEXECUTION" in query["sql"].upper()
        and "CASE WHEN" in query["sql"].upper()
    ]
    assert bounded_queue_queries
    assert all("LENGTH(" in query for query in bounded_queue_queries)
    assert all(oversized_queue not in query for query in bounded_queue_queries)
    assert [group.queue for group in report.nonterminal_work.groups] == ["[OVERSIZED]"]
    assert oversized_queue not in render_protocol_status_json(report)
    assert oversized_queue not in render_protocol_status_text(report)


def test_overlapping_live_ranges_do_not_double_count_supported_work() -> None:
    observed_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    _close_legacy_admission()
    _explicit_lease(
        "status-overlap-narrow",
        minimum=2,
        maximum=3,
        heartbeat_at=observed_at,
        queue="unrelated-one",
    )
    _explicit_lease(
        "status-overlap-wide",
        minimum=1,
        maximum=4,
        heartbeat_at=observed_at,
        package_version="definitely-not-semver",
        queue="unrelated-two",
    )
    _execution("status-overlap-task", protocol=2, queue="target-queue")

    report = build_protocol_status(using="default", observed_at=observed_at)

    assert report.capabilities.total_groups == 2
    assert report.capabilities.total_leases == 2
    assert report.nonterminal_work.total_tasks == 1
    assert report.unsupported_work.total_tasks == 0
    assert ProtocolStatusBlockerCode.UNSUPPORTED_NONTERMINAL_WORK not in {
        blocker.code for blocker in report.blockers
    }


def test_protocol_status_command_json_and_text_render_the_same_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = build_protocol_status(
        using="default",
        observed_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    )
    selected_databases: list[str] = []

    def fixed_report(*, using: str) -> ProtocolStatusReport:
        selected_databases.append(using)
        return report

    monkeypatch.setattr(protocol_status_command, "build_protocol_status", fixed_report)

    json_output = StringIO()
    call_command(
        "django_ray_protocol_status",
        "--database=default",
        "--json",
        stdout=json_output,
    )
    encoded = json_output.getvalue().rstrip("\n")
    assert encoded == render_protocol_status_json(report)
    assert json.loads(encoded) == protocol_status_to_dict(report)

    text_output = StringIO()
    call_command(
        "django_ray_protocol_status",
        "--database=default",
        stdout=text_output,
    )
    assert text_output.getvalue().rstrip("\n") == render_protocol_status_text(report)
    assert selected_databases == ["default", "default"]


def test_protocol_status_command_succeeds_with_fixed_external_blockers() -> None:
    output = StringIO()

    call_command("django_ray_protocol_status", "--json", stdout=output)

    encoded = output.getvalue().rstrip("\n")
    payload = json.loads(encoded)
    assert payload["schema"] == PROTOCOL_STATUS_SCHEMA
    assert payload["schema_version"] == PROTOCOL_STATUS_SCHEMA_VERSION
    assert len(encoded.encode("utf-8")) < PROTOCOL_STATUS_OUTPUT_MAX_BYTES
    assert {blocker["code"] for blocker in payload["blockers"]}.issuperset(
        {
            ProtocolStatusBlockerCode.LEGACY_PRODUCERS_UNATTESTED.value,
            ProtocolStatusBlockerCode.LEGACY_READERS_UNATTESTED.value,
            ProtocolStatusBlockerCode.QUEUE_CAPACITY_UNATTESTED.value,
        }
    )


def test_protocol_status_command_rejects_an_unknown_database_alias() -> None:
    with pytest.raises(CommandError, match="unavailable"):
        call_command("django_ray_protocol_status", "--database=missing")


def test_protocol_status_rejects_a_caller_owned_atomic_transaction() -> None:
    with transaction.atomic(using="default"):
        with pytest.raises(ProtocolStatusError, match="must own its outermost"):
            build_protocol_status(
                using="default",
                observed_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
            )
        assert connection.in_atomic_block is True
        assert TaskExecutionProtocolPolicy.objects.get(singleton_key=1).revision == 1


def test_protocol_status_rejects_caller_disabled_autocommit() -> None:
    transaction.set_autocommit(False, using="default")
    try:
        with pytest.raises(ProtocolStatusError, match="must own its outermost"):
            build_protocol_status(
                using="default",
                observed_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
            )
        connection.rollback()
    finally:
        transaction.set_autocommit(True, using="default")

    assert connection.get_autocommit() is True
    assert TaskExecutionProtocolPolicy.objects.get(singleton_key=1).revision == 1


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("missing-policy", "policy singleton is unavailable"),
        ("open-without-token", "policy and token are inconsistent"),
        ("closed-with-token", "policy and token are inconsistent"),
    ],
)
def test_protocol_status_corruption_fails_closed_without_mutation(
    corruption: str,
    message: str,
) -> None:
    if corruption == "missing-policy":
        TaskExecutionProtocolPolicy.objects.get(singleton_key=1).delete()
    elif corruption == "open-without-token":
        LegacyWorkerAdmissionToken.objects.get(singleton_key=1).delete()
    else:
        TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
            legacy_worker_admission_enabled=False,
            revision=2,
        )
    before = _database_snapshot()

    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(ProtocolStatusError, match=message):
            build_protocol_status(
                using="default",
                observed_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
            )

    _assert_read_only(captured.captured_queries)
    assert _database_snapshot() == before
    with pytest.raises(CommandError, match=message):
        call_command("django_ray_protocol_status", "--json")
    assert _database_snapshot() == before


def test_revision_exhaustion_and_nonterminal_rollback_blocker_are_distinct() -> None:
    maximum_revision = (1 << 63) - 1
    _execution(
        "status-terminal-v2",
        protocol=2,
        queue="terminal",
        state=TaskState.SUCCEEDED,
    )
    TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(revision=maximum_revision)

    terminal_only = build_protocol_status(observed_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC))

    terminal_blockers = {blocker.code: blocker.count for blocker in terminal_only.blockers}
    assert terminal_only.non_v1_nonterminal_count == 0
    assert terminal_only.nonterminal_work.total_tasks == 0
    assert terminal_blockers[ProtocolStatusBlockerCode.POLICY_REVISION_EXHAUSTED] == 1
    assert ProtocolStatusBlockerCode.NON_V1_NONTERMINAL_WORK not in terminal_blockers

    LegacyWorkerAdmissionToken.objects.get(singleton_key=1).delete()
    TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
        legacy_worker_admission_enabled=False,
    )
    _execution("status-nonterminal-v2", protocol=2, queue="future")

    nonterminal = build_protocol_status(observed_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC))
    nonterminal_blockers = {blocker.code: blocker.count for blocker in nonterminal.blockers}
    assert nonterminal.non_v1_nonterminal_count == 1
    assert nonterminal_blockers[ProtocolStatusBlockerCode.NON_V1_NONTERMINAL_WORK] == 1
    assert nonterminal_blockers[ProtocolStatusBlockerCode.POLICY_REVISION_EXHAUSTED] == 1
