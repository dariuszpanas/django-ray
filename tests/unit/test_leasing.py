"""Unit tests for worker leasing functionality."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from django_ray.models import TaskWorkerLease
from django_ray.runner.leasing import (
    cleanup_expired_leases,
    generate_worker_id,
    get_active_worker_count,
    get_active_workers,
    get_heartbeat_interval,
    get_lease_duration,
    is_lease_expired,
    release_lease,
)


class TestWorkerIdGeneration:
    """Tests for worker ID generation."""

    def test_generate_worker_id_returns_string(self) -> None:
        """Test that generate_worker_id returns a string."""
        worker_id = generate_worker_id()
        assert isinstance(worker_id, str)

    def test_generate_worker_id_unique(self) -> None:
        """Test that generated worker IDs are unique."""
        ids = [generate_worker_id() for _ in range(100)]
        assert len(set(ids)) == 100, "Worker IDs should be unique"

    def test_generate_worker_id_valid_uuid(self) -> None:
        """Test that worker ID is a valid UUID format."""
        import uuid

        worker_id = generate_worker_id()
        # Should not raise
        uuid.UUID(worker_id)


class TestLeaseDurations:
    """Tests for lease duration settings."""

    def test_get_lease_duration_returns_timedelta(self) -> None:
        """Test that get_lease_duration returns a timedelta."""
        duration = get_lease_duration()
        assert isinstance(duration, timedelta)

    def test_get_heartbeat_interval_returns_timedelta(self) -> None:
        """Test that get_heartbeat_interval returns a timedelta."""
        interval = get_heartbeat_interval()
        assert isinstance(interval, timedelta)

    def test_heartbeat_interval_less_than_lease(self) -> None:
        """Test that heartbeat interval is shorter than lease duration."""
        interval = get_heartbeat_interval()
        duration = get_lease_duration()
        assert interval < duration, "Heartbeat should be more frequent than lease expiry"


@pytest.mark.django_db
class TestLeaseExpiration:
    """Tests for lease expiration checking."""

    def test_fresh_lease_not_expired(self) -> None:
        """Test that a freshly created lease is not expired."""
        lease = TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="test-host",
            pid=12345,
            queue_name="default",
        )

        assert not is_lease_expired(lease)

    def test_old_lease_is_expired(self) -> None:
        """Test that an old lease is detected as expired."""
        lease = TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="test-host",
            pid=12345,
            queue_name="default",
        )

        # Set heartbeat to well in the past
        old_time = datetime.now(UTC) - timedelta(hours=1)
        lease.last_heartbeat_at = old_time
        lease.save(update_fields=["last_heartbeat_at"])

        assert is_lease_expired(lease)

    def test_recently_updated_lease_not_expired(self) -> None:
        """Test that a recently updated lease is not expired."""
        lease = TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="test-host",
            pid=12345,
            queue_name="default",
        )

        # Update heartbeat to now
        lease.last_heartbeat_at = datetime.now(UTC)
        lease.save(update_fields=["last_heartbeat_at"])

        assert not is_lease_expired(lease)

    def test_inactive_lease_is_expired(self) -> None:
        """Test that an inactive lease is always considered expired."""
        lease = TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="test-host",
            pid=12345,
            queue_name="default",
            is_active=False,
        )

        # Even with recent heartbeat, inactive lease should be expired
        assert is_lease_expired(lease)


@pytest.mark.django_db
class TestLeaseLifecycle:
    """Tests for worker lease lifecycle."""

    def test_create_lease(self) -> None:
        """Test creating a worker lease."""
        worker_id = generate_worker_id()
        lease = TaskWorkerLease.objects.create(
            worker_id=worker_id,
            hostname="test-host",
            pid=12345,
            queue_name="default",
        )

        assert lease.worker_id == worker_id
        assert lease.hostname == "test-host"
        assert lease.pid == 12345
        assert lease.queue_name == "default"
        assert lease.started_at is not None
        assert lease.last_heartbeat_at is not None

    def test_update_lease_heartbeat(self) -> None:
        """Test updating lease heartbeat."""
        lease = TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="test-host",
            pid=12345,
            queue_name="default",
        )

        original_heartbeat = lease.last_heartbeat_at

        # Update heartbeat
        new_time = datetime.now(UTC)
        lease.last_heartbeat_at = new_time
        lease.save(update_fields=["last_heartbeat_at"])

        # Refresh from DB
        lease.refresh_from_db()

        assert lease.last_heartbeat_at >= original_heartbeat

    def test_delete_lease(self) -> None:
        """Test deleting a worker lease."""
        worker_id = generate_worker_id()
        lease = TaskWorkerLease.objects.create(
            worker_id=worker_id,
            hostname="test-host",
            pid=12345,
            queue_name="default",
        )

        assert TaskWorkerLease.objects.filter(worker_id=worker_id).exists()

        lease.delete()

        assert not TaskWorkerLease.objects.filter(worker_id=worker_id).exists()

    def test_multiple_leases_per_queue(self) -> None:
        """Test that multiple workers can have leases on the same queue."""
        lease1 = TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="host-1",
            pid=12345,
            queue_name="default",
        )
        lease2 = TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="host-2",
            pid=12346,
            queue_name="default",
        )

        assert TaskWorkerLease.objects.filter(queue_name="default").count() == 2

    def test_leases_on_different_queues(self) -> None:
        """Test workers on different queues have separate leases."""
        lease1 = TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="host-1",
            pid=12345,
            queue_name="default",
        )
        lease2 = TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="host-2",
            pid=12346,
            queue_name="high-priority",
        )

        assert TaskWorkerLease.objects.filter(queue_name="default").count() == 1
        assert TaskWorkerLease.objects.filter(queue_name="high-priority").count() == 1


@pytest.mark.django_db
class TestLeaseCleanup:
    """Tests for lease cleanup functionality."""

    def test_cleanup_expired_leases_removes_old(self) -> None:
        """Test that cleanup marks expired leases as inactive."""
        # Create an expired lease
        old_time = datetime.now(UTC) - timedelta(hours=1)
        expired_lease = TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="test-host",
            pid=12345,
            queue_name="default",
            last_heartbeat_at=old_time,
        )

        # Create a fresh lease
        fresh_lease = TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="test-host-2",
            pid=12346,
            queue_name="default",
        )

        # Cleanup should mark the expired one as inactive
        inactive_count = cleanup_expired_leases()

        assert inactive_count == 1

        # Expired lease should still exist but be marked inactive
        expired_lease.refresh_from_db()
        assert not expired_lease.is_active
        assert expired_lease.stopped_at is not None

        # Fresh lease should still be active
        fresh_lease.refresh_from_db()
        assert fresh_lease.is_active

    def test_cleanup_expired_leases_none_expired(self) -> None:
        """Test cleanup when no leases are expired."""
        # Create only fresh leases
        TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="test-host",
            pid=12345,
            queue_name="default",
        )

        inactive_count = cleanup_expired_leases()
        assert inactive_count == 0

    def test_release_lease_success(self) -> None:
        """Test releasing a lease by worker ID marks it inactive."""
        worker_id = generate_worker_id()
        TaskWorkerLease.objects.create(
            worker_id=worker_id,
            hostname="test-host",
            pid=12345,
            queue_name="default",
        )

        result = release_lease(worker_id)

        assert result is True
        # Lease should still exist but be marked inactive
        lease = TaskWorkerLease.objects.get(worker_id=worker_id)
        assert not lease.is_active
        assert lease.stopped_at is not None

    def test_release_lease_not_found(self) -> None:
        """Test releasing a non-existent lease."""
        result = release_lease("non-existent-worker-id")
        assert result is False


@pytest.mark.django_db
class TestActiveWorkers:
    """Tests for active worker queries."""

    def test_get_active_worker_count(self) -> None:
        """Test counting active workers."""
        # Create 2 active leases
        TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="host-1",
            pid=12345,
            queue_name="default",
        )
        TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="host-2",
            pid=12346,
            queue_name="default",
        )
        # Create 1 lease with expired heartbeat
        old_time = datetime.now(UTC) - timedelta(hours=1)
        TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="host-3",
            pid=12347,
            queue_name="default",
            last_heartbeat_at=old_time,
        )
        # Create 1 inactive lease
        TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="host-4",
            pid=12348,
            queue_name="default",
            is_active=False,
        )

        count = get_active_worker_count()
        assert count == 2

    def test_get_active_workers(self) -> None:
        """Test getting list of active workers."""
        # Create 1 active lease
        active_id = generate_worker_id()
        TaskWorkerLease.objects.create(
            worker_id=active_id,
            hostname="host-1",
            pid=12345,
            queue_name="default",
        )
        # Create 1 lease with expired heartbeat
        old_time = datetime.now(UTC) - timedelta(hours=1)
        TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="host-2",
            pid=12346,
            queue_name="default",
            last_heartbeat_at=old_time,
        )
        # Create 1 inactive lease
        TaskWorkerLease.objects.create(
            worker_id=generate_worker_id(),
            hostname="host-3",
            pid=12347,
            queue_name="default",
            is_active=False,
        )

        workers = get_active_workers()
        assert len(workers) == 1
        assert workers[0].worker_id == active_id
