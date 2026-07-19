"""Regression coverage for Django admin boundary behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.contrib import admin
from django.test import RequestFactory

from django_ray.admin import ActiveWorkerFilter, TaskWorkerLeaseAdmin
from django_ray.models import TaskWorkerLease


def _request() -> Any:
    return RequestFactory().get("/admin/")


def _lease_admin() -> TaskWorkerLeaseAdmin:
    return TaskWorkerLeaseAdmin(TaskWorkerLease, admin.site)


class _ChangeList:
    def get_query_string(self, params: dict[str, str]) -> str:
        key, value = next(iter(params.items()))
        return f"?{key}={value}"


@pytest.mark.django_db
class TestAdminCoverage:
    def test_active_worker_filter_choices_select_default_and_requested_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        filter_obj = object.__new__(ActiveWorkerFilter)
        filter_obj.lookup_choices = [
            ("active", "Active"),
            ("inactive", "Inactive"),
            ("all", "All"),
        ]
        monkeypatch.setattr(filter_obj, "value", lambda: None)

        default_choices = list(filter_obj.choices(_ChangeList()))

        assert default_choices == [
            {"selected": True, "query_string": "?is_active=active", "display": "Active"},
            {
                "selected": False,
                "query_string": "?is_active=inactive",
                "display": "Inactive",
            },
            {"selected": False, "query_string": "?is_active=all", "display": "All"},
        ]

        monkeypatch.setattr(filter_obj, "value", lambda: "inactive")
        requested_choices = list(filter_obj.choices(_ChangeList()))

        assert [choice["selected"] for choice in requested_choices] == [False, True, False]

    def test_lease_admin_reports_expired_lease_and_returns_base_queryset(self) -> None:
        recent = TaskWorkerLease.objects.create(
            worker_id="admin-coverage-recent",
            hostname="admin-host",
            pid=6001,
            queue_name="default",
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=125),
            is_active=True,
        )
        expired = TaskWorkerLease.objects.create(
            worker_id="admin-coverage-expired",
            hostname="admin-host",
            pid=6002,
            queue_name="default",
            last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=5),
            is_active=True,
        )
        admin_obj = _lease_admin()

        queryset = admin_obj.get_queryset(_request())

        assert set(queryset.values_list("worker_id", flat=True)) == {
            recent.worker_id,
            expired.worker_id,
        }
        assert admin_obj.is_active_display_list(expired) is False
        assert admin_obj.time_since_heartbeat(recent).startswith("2m ")
