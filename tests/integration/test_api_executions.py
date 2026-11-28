"""Integration tests for the Django Ninja /api/executions/* endpoints."""

from __future__ import annotations

import json
import warnings
from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from django_ray.lifecycle import (
    TaskCancellationRequestResult,
    TaskCancellationRequestStatus,
    TaskRetryRequestResult,
    TaskRetryRequestStatus,
)
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease
from django_ray.protocol_coordination import close_legacy_worker_admission
from django_ray.redaction import REDACTED, redact_text
from django_ray.workflow.progress.summary import serialize_workflow_progress_summary
from testproject import api as testproject_api
from tests.integration._api_helpers import build_demo_client
from tests.workflow_progress_summary_helpers import workflow_progress_summary


@pytest.fixture
def client(settings, db) -> Client:
    """Explicitly opted-in demo client for existing positive route coverage."""
    return build_demo_client(settings)


@pytest.fixture
def unauthenticated_client() -> Client:
    return Client()


@pytest.mark.django_db
class TestExecutionsAPI:
    """Test the /api/executions/* admin endpoints."""

    def test_list_executions_empty(self, client):
        """Test listing executions when none exist."""
        response = client.get("/api/executions")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["tasks"] == []
        assert data["limit"] == testproject_api._EXECUTION_LIST_DEFAULT_LIMIT
        assert data["returned_count"] == 0
        assert data["has_more"] is False
        assert data["next_cursor"] is None
        assert data["truncated"] is False
        assert data["truncation_reason"] is None
        assert data["diagnostic_max_bytes"] == (
            testproject_api._EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES
        )
        assert data["response_max_bytes"] == (testproject_api._EXECUTION_LIST_RESPONSE_MAX_BYTES)

    def test_list_executions_with_data(self, client):
        """Test listing executions with existing tasks."""
        RayTaskExecution.objects.create(
            task_id="test-1",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
        )
        RayTaskExecution.objects.create(
            task_id="test-2",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.SUCCEEDED,
        )
        deadline = datetime.now(UTC) - timedelta(seconds=1)
        RayTaskExecution.objects.create(
            task_id="test-expired-list",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.EXPIRED,
            queue_timeout_seconds=60,
            queue_deadline_at=deadline,
        )

        response = client.get("/api/executions")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert data["queued"] == 1
        assert data["succeeded"] == 1
        assert data["expired"] == 1
        expired = next(task for task in data["tasks"] if task["state"] == TaskState.EXPIRED)
        assert expired["queue_timeout_seconds"] == 60
        assert abs(datetime.fromisoformat(expired["queue_deadline_at"]) - deadline) < timedelta(
            milliseconds=1
        )

    def test_protocol_visibility_is_consistent_across_bounded_surfaces(self, client):
        """All execution reads expose one fixed protocol-capacity projection."""
        TaskWorkerLease.objects.create(
            worker_id="protocol-api-reader",
            hostname="protocol-api-host",
            pid=4101,
            queue_name="another-queue",
            capability_schema_version=1,
            django_ray_version="0.5.0-reader",
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            legacy_admission_token=None,
        )
        task = RayTaskExecution.objects.create(
            task_id="protocol-visible-api-task",
            callable_path="test.task",
            queue_name="target-queue",
            created_with_django_ray_version="0.5.0-producer",
            managed_with_django_ray_version="0.5.0-manager",
            executor_django_ray_version="0.5.0-executor",
        )

        status = client.get(f"/api/tasks/{task.task_id}")
        listing = client.get("/api/executions", {"task_id": task.task_id})
        detail = client.get(f"/api/executions/{task.pk}")

        assert status.status_code == listing.status_code == detail.status_code == 200
        list_item = listing.json()["tasks"][0]
        for payload in (status.json(), list_item, detail.json()):
            assert payload["execution_protocol_version"] == 1
            assert payload["created_with_django_ray_version"] == "0.5.0-producer"
            assert payload["managed_with_django_ray_version"] == "0.5.0-manager"
            assert payload["executor_django_ray_version"] == "0.5.0-executor"
            assert payload["protocol_compatible_worker_available"] is True
            assert payload["queue_capacity_attested"] is False
        assert len(status.content) <= testproject_api._TASK_STATUS_RESPONSE_MAX_BYTES
        assert len(listing.content) <= testproject_api._EXECUTION_LIST_RESPONSE_MAX_BYTES
        assert len(detail.content) <= testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES

    def test_protocol_provenance_is_sql_bounded_before_sqlite_transfer(self, client):
        if connection.vendor != "sqlite":
            pytest.skip("SQLite corruption guard is specific to unenforced VARCHAR lengths")
        oversized = "CANARY_OVERSIZED_PROVENANCE_" + "x" * 256
        embedded_nul = "CANARY_NUL_PROVENANCE\x00" + "y" * 256
        exact = "m" * testproject_api._EXECUTION_PROVENANCE_MAX_BYTES
        task = RayTaskExecution.objects.create(
            task_id="protocol-oversized-provenance",
            callable_path="test.task",
            created_with_django_ray_version=oversized,
            managed_with_django_ray_version=exact,
            executor_django_ray_version=embedded_nul,
        )

        status = client.get(f"/api/tasks/{task.task_id}")
        listing = client.get("/api/executions", {"task_id": task.task_id})
        detail = client.get(f"/api/executions/{task.pk}")

        assert status.status_code == listing.status_code == detail.status_code == 200
        for response, payload in (
            (status, status.json()),
            (listing, listing.json()["tasks"][0]),
            (detail, detail.json()),
        ):
            assert payload["created_with_django_ray_version"] is None
            assert payload["managed_with_django_ray_version"] == exact
            assert payload["executor_django_ray_version"] is None
            assert "CANARY_" not in response.content.decode()

    def test_protocol_provenance_uses_configured_redaction_on_every_surface(
        self,
        client,
        settings,
    ):
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"password"]}
        task = RayTaskExecution.objects.create(
            task_id="protocol-redacted-provenance",
            callable_path="test.task",
            created_with_django_ray_version="password=producer-secret",
            managed_with_django_ray_version="password=manager-secret",
            executor_django_ray_version="password=executor-secret",
        )

        status = client.get(f"/api/tasks/{task.task_id}")
        listing = client.get("/api/executions", {"task_id": task.task_id})
        detail = client.get(f"/api/executions/{task.pk}")

        for response, payload in (
            (status, status.json()),
            (listing, listing.json()["tasks"][0]),
            (detail, detail.json()),
        ):
            assert payload["created_with_django_ray_version"] == REDACTED
            assert payload["managed_with_django_ray_version"] == REDACTED
            assert payload["executor_django_ray_version"] == REDACTED
            assert "secret" not in response.content.decode()

    def test_execution_counts_ignore_unknown_database_states(self):
        RayTaskExecution.objects.create(
            task_id="known-count-state",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )
        RayTaskExecution.objects.create(
            task_id="unknown-count-state",
            callable_path="test.task",
            state="CANARY_UNKNOWN_STATE_" + "x" * 256,
        )

        counts = testproject_api._task_state_counts()

        assert counts == {TaskState.QUEUED: 1}

    def test_list_executions_filter_by_state(self, client):
        """Test filtering executions by state."""
        RayTaskExecution.objects.create(
            task_id="test-1",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )
        RayTaskExecution.objects.create(
            task_id="test-2",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
        )

        response = client.get("/api/executions?state=QUEUED")
        assert response.status_code == 200
        data = response.json()
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["state"] == "QUEUED"

    def test_list_executions_filter_by_exact_task_id(self, client):
        """A caller can poll one execution without fetching the recent history."""
        RayTaskExecution.objects.create(
            task_id="gate-target",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data="5",
        )
        RayTaskExecution.objects.create(
            task_id="unrelated",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data="99",
        )

        response = client.get("/api/executions?task_id=gate-target&limit=1")

        assert response.status_code == 200
        assert [task["task_id"] for task in response.json()["tasks"]] == ["gate-target"]

    @pytest.mark.parametrize("filter_name", ("state", "queue", "task_id"))
    def test_list_executions_rejects_nul_filters_before_query(
        self,
        client,
        monkeypatch: pytest.MonkeyPatch,
        filter_name: str,
    ) -> None:
        def forbidden_query():
            raise AssertionError("Invalid filters must fail before constructing the query")

        monkeypatch.setattr(testproject_api, "_workflow_observability_executions", forbidden_query)

        response = client.get("/api/executions", {filter_name: "NUL_CANARY\x00"})

        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid execution-list filter."}
        assert b"NUL_CANARY" not in response.content

    def test_list_executions_preserves_unicode_historical_filters(self, client) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="historical-東京",
            callable_path="test.task",
            queue_name="retired-Δ",
            state=TaskState.SUCCEEDED,
        )
        RayTaskExecution.objects.create(
            task_id="unrelated-history",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
        )

        responses = (
            client.get("/api/executions", {"queue": "retired-Δ"}),
            client.get("/api/executions", {"task_id": "historical-東京"}),
        )

        for response in responses:
            assert response.status_code == 200
            assert [row["id"] for row in response.json()["tasks"]] == [execution.pk]

    def test_list_executions_preserves_empty_filters(self, client) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="empty-filter-history",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )

        response = client.get("/api/executions", {"state": "", "queue": "", "task_id": ""})

        assert response.status_code == 200
        assert [row["id"] for row in response.json()["tasks"]] == [execution.pk]

    @pytest.mark.parametrize(
        "limit",
        (
            "not-an-integer",
            "0",
            "-1",
            str(testproject_api._EXECUTION_LIST_MAX_LIMIT + 1),
        ),
    )
    def test_list_executions_rejects_invalid_page_sizes(self, client, limit):
        response = client.get("/api/executions", {"limit": limit})

        assert response.status_code == 422

    @pytest.mark.parametrize(
        "cursor",
        (
            "not-a-signed-cursor",
            "x" * (testproject_api._EXECUTION_LIST_CURSOR_MAX_CHARACTERS + 1),
        ),
    )
    def test_list_executions_rejects_invalid_cursors(self, client, cursor):
        response = client.get("/api/executions", {"cursor": cursor})

        assert response.status_code == 422

    def test_execution_list_openapi_documents_page_bounds(self, client):
        operation = client.get("/api/openapi.json").json()["paths"]["/api/executions"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

        limit = parameters["limit"]["schema"]
        assert limit["minimum"] == testproject_api._EXECUTION_LIST_MIN_LIMIT
        assert limit["maximum"] == testproject_api._EXECUTION_LIST_MAX_LIMIT
        assert limit["default"] == testproject_api._EXECUTION_LIST_DEFAULT_LIMIT

        cursor = parameters["cursor"]["schema"]
        cursor_string = next(item for item in cursor["anyOf"] if item.get("type") == "string")
        assert cursor_string["maxLength"] == (testproject_api._EXECUTION_LIST_CURSOR_MAX_CHARACTERS)
        assert parameters["cursor"]["required"] is False

    def test_list_executions_paginates_with_stable_continuation_metadata(self, client):
        tasks = RayTaskExecution.objects.bulk_create(
            [
                RayTaskExecution(
                    task_id=f"paged-{index}",
                    callable_path="test.task",
                    state=TaskState.QUEUED,
                )
                for index in range(5)
            ]
        )

        first = client.get("/api/executions?limit=2")
        first_data = first.json()
        inserted = RayTaskExecution.objects.create(
            task_id="paged-concurrent-insert",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )
        second = client.get(
            "/api/executions",
            {"limit": 2, "cursor": first_data["next_cursor"]},
        )
        second_data = second.json()
        third = client.get(
            "/api/executions",
            {"limit": 2, "cursor": second_data["next_cursor"]},
        )

        assert first.status_code == second.status_code == third.status_code == 200
        third_data = third.json()
        assert first_data["total"] == 5
        assert second_data["total"] == third_data["total"] == 6
        assert first_data["returned_count"] == second_data["returned_count"] == 2
        assert first_data["has_more"] is second_data["has_more"] is True
        assert isinstance(first_data["next_cursor"], str)
        assert isinstance(second_data["next_cursor"], str)
        assert first_data["next_cursor"] != second_data["next_cursor"]
        assert first_data["truncation_reason"] == "page_limit"
        assert second_data["truncation_reason"] == "page_limit"
        assert third_data["returned_count"] == 1
        assert third_data["has_more"] is False
        assert third_data["next_cursor"] is None
        assert third_data["truncation_reason"] is None
        returned_ids = {
            item["id"] for page in (first_data, second_data, third_data) for item in page["tasks"]
        }
        assert returned_ids == {task.pk for task in tasks}
        assert inserted.pk not in returned_ids

    def test_execution_list_cursor_is_tamper_evident_and_filter_bound(self, client):
        for index in range(2):
            RayTaskExecution.objects.create(
                task_id=f"cursor-queued-{index}",
                callable_path="test.task",
                state=TaskState.QUEUED,
            )
        RayTaskExecution.objects.create(
            task_id="cursor-succeeded",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
        )
        first = client.get("/api/executions", {"state": "queued", "limit": 1})
        cursor = first.json()["next_cursor"]
        assert isinstance(cursor, str)

        tamper_index = len(cursor) // 2
        replacement = "A" if cursor[tamper_index] != "A" else "B"
        tampered = cursor[:tamper_index] + replacement + cursor[tamper_index + 1 :]

        assert (
            client.get(
                "/api/executions",
                {"state": "queued", "cursor": tampered},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/executions",
                {"state": "SUCCEEDED", "cursor": cursor},
            ).status_code
            == 422
        )

    def test_list_executions_omits_oversized_diagnostics_in_the_database_query(
        self,
        client,
    ):
        oversized = "x\x00" + "y" * testproject_api._EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES
        task = RayTaskExecution.objects.create(
            task_id="oversized-list-diagnostics",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data=oversized,
            error_message=oversized,
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/executions?task_id=oversized-list-diagnostics")

        assert response.status_code == 200
        item = response.json()["tasks"][0]
        assert item["result_data"] is None
        assert item["error_message"] is None
        assert item["result_data_omission_reason"] == "stored_value_exceeds_list_limit"
        assert item["error_message_omission_reason"] == "stored_value_exceeds_list_limit"
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 2
        guarded_select = next(query for query in task_selects if "CASE WHEN" in query)
        assert "LENGTH(CAST(" in guarded_select or "OCTET_LENGTH(" in guarded_select
        assert 'THEN "django_ray_raytaskexecution"."result_data"' in guarded_select
        assert 'THEN "django_ray_raytaskexecution"."error_message"' in guarded_select

        detail = client.get(f"/api/executions/{task.pk}")
        assert detail.status_code == 200
        detail_payload = detail.json()
        assert detail_payload["result_data"] == REDACTED
        assert detail_payload["error_message"] == redact_text(oversized)
        assert detail_payload["result_data_omission_reason"] is None
        assert detail_payload["error_message_omission_reason"] is None

    def test_execution_list_fails_clearly_on_an_unsupported_database(self, monkeypatch):
        monkeypatch.setattr(connection, "vendor", "oracle")

        with pytest.raises(ImproperlyConfigured, match="only SQLite and PostgreSQL"):
            testproject_api._bounded_execution_list_rows(
                RayTaskExecution.objects.all(),
                limit=1,
            )

    def test_execution_detail_fails_clearly_on_an_unsupported_database(self, monkeypatch):
        monkeypatch.setattr(connection, "vendor", "oracle")

        with pytest.raises(
            ImproperlyConfigured,
            match="execution detail supports only SQLite and PostgreSQL",
        ):
            testproject_api._bounded_execution_detail_row(1)

    def test_list_executions_enforces_the_aggregate_encoded_response_bound(
        self,
        client,
        monkeypatch,
    ):
        diagnostic = "x" * (testproject_api._EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES - 102)
        serialized_result = json.dumps(diagnostic)
        assert len(serialized_result.encode()) == (
            testproject_api._EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES - 100
        )
        tasks = RayTaskExecution.objects.bulk_create(
            [
                RayTaskExecution(
                    task_id=f"aggregate-bound-{index}",
                    callable_path="test.task",
                    state=TaskState.FAILED,
                    result_data=serialized_result,
                    error_message=diagnostic,
                )
                for index in range(40)
            ]
        )
        validation_calls = {"item": 0, "result": 0, "error": 0}
        real_execution_list_item = testproject_api._execution_list_item
        real_safe_json_dumps = testproject_api.safe_json_dumps
        real_redact_text = testproject_api.redact_text

        def counted_execution_list_item(row):
            validation_calls["item"] += 1
            return real_execution_list_item(row)

        def counted_safe_json_dumps(value, **kwargs):
            validation_calls["result"] += 1
            return real_safe_json_dumps(value, **kwargs)

        def counted_redact_text(value, **kwargs):
            validation_calls["error"] += 1
            return real_redact_text(value, **kwargs)

        monkeypatch.setattr(
            testproject_api,
            "_execution_list_item",
            counted_execution_list_item,
        )
        monkeypatch.setattr(testproject_api, "safe_json_dumps", counted_safe_json_dumps)
        monkeypatch.setattr(testproject_api, "redact_text", counted_redact_text)

        first = client.get(
            "/api/executions",
            {"limit": testproject_api._EXECUTION_LIST_MAX_LIMIT},
        )
        repeated = client.get(
            "/api/executions",
            {"limit": testproject_api._EXECUTION_LIST_MAX_LIMIT},
        )

        assert first.status_code == 200
        assert first.content == repeated.content
        assert len(first.content) <= testproject_api._EXECUTION_LIST_RESPONSE_MAX_BYTES
        first_data = first.json()
        assert 0 < first_data["returned_count"] < len(tasks)
        assert first_data["has_more"] is True
        assert first_data["truncated"] is True
        assert first_data["truncation_reason"] == "response_size_limit"
        assert isinstance(first_data["next_cursor"], str)
        assert all(
            item["result_data_omission_reason"] is None
            and item["error_message_omission_reason"] is None
            for item in first_data["tasks"]
        )

        second = client.get(
            "/api/executions",
            {
                "limit": testproject_api._EXECUTION_LIST_MAX_LIMIT,
                "cursor": first_data["next_cursor"],
            },
        )
        assert second.status_code == 200
        assert len(second.content) <= testproject_api._EXECUTION_LIST_RESPONSE_MAX_BYTES
        second_data = second.json()
        first_ids = {item["id"] for item in first_data["tasks"]}
        second_ids = {item["id"] for item in second_data["tasks"]}
        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == {task.pk for task in tasks}
        assert validation_calls["item"] > 0
        assert validation_calls["result"] == validation_calls["item"]
        assert validation_calls["error"] == validation_calls["item"]

    def test_get_execution(self, client):
        """Test getting a specific execution by internal ID."""
        task = RayTaskExecution.objects.create(
            task_id="test-get",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
        )

        response = client.get(f"/api/executions/{task.pk}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == task.pk
        assert data["callable_path"] == "testproject.tasks.add_numbers"
        assert data["result_data_omission_reason"] is None
        assert data["error_message_omission_reason"] is None
        assert data["diagnostic_max_bytes"] == (
            testproject_api._EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
        )
        assert data["response_max_bytes"] == (testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_execution_detail_uses_one_public_projection_without_loading_external_result(
        self,
        client,
        monkeypatch,
    ):
        protected_marker = "protected-execution-detail-marker"
        task = RayTaskExecution.objects.create(
            task_id="test-detail-public-projection",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            args_json=f'["{protected_marker}-args"]',
            kwargs_json=f'{{"secret":"{protected_marker}-kwargs"}}',
            input_reference=f"digest:{protected_marker}-input",
            result_reference=f"digest:{protected_marker}-result",
            runtime_env_json=f'{{"secret":"{protected_marker}-runtime"}}',
            progress_data=f'{{"secret":"{protected_marker}-progress"}}',
            workflow_progress_summary_json=(f'{{"secret":"{protected_marker}-summary"}}'),
            workflow_plan_json=f'{{"secret":"{protected_marker}-plan"}}',
            workflow_plan_selection=f'{{"secret":"{protected_marker}-selection"}}',
            completion_data=f'{{"secret":"{protected_marker}-completion"}}',
            cancellation_error=f"{protected_marker}-cancellation",
            error_traceback=f"{protected_marker}-traceback",
        )

        def reject_external_load(*args, **kwargs):
            raise AssertionError("execution detail must not load external result storage")

        monkeypatch.setattr(
            "django_ray.result_storage.load_result_reference",
            reject_external_load,
        )
        captured_rows = []
        original_item = testproject_api._execution_detail_item

        def capture_item(row):
            captured_rows.append(dict(row))
            return original_item(row)

        monkeypatch.setattr(testproject_api, "_execution_detail_item", capture_item)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["result_data"] is None
        assert payload["result_data_omission_reason"] == "external_result_not_loaded"
        assert protected_marker not in response.content.decode()
        assert len(captured_rows) == 1
        assert set(captured_rows[0]) == set(testproject_api._EXECUTION_DETAIL_VALUE_FIELDS)
        assert captured_rows[0]["_detail_has_result_reference"] is True
        assert "result_reference" not in captured_rows[0]

        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 1
        detail_sql = task_selects[0]
        assert "result_reference" in detail_sql
        assert "FOR UPDATE" not in detail_sql.upper()
        for protected_field in (
            "args_json",
            "kwargs_json",
            "input_reference",
            "runtime_env_json",
            "progress_data",
            "workflow_progress_summary_json",
            "workflow_plan_json",
            "workflow_plan_selection",
            "completion_data",
            "cancellation_error",
            "error_traceback",
        ):
            assert protected_field not in detail_sql

    def test_execution_detail_enforces_embedded_nul_byte_boundary_before_transfer(
        self,
        client,
        monkeypatch,
    ):
        limit = testproject_api._EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
        exact_value = "x\x00" + "y" * (limit - 2)
        protected_marker = "oversized-detail-secret-marker"
        oversized_value = "x\x00" + "y" * (limit - len(protected_marker)) + protected_marker
        assert len(exact_value.encode()) == limit
        assert len(oversized_value.encode()) == limit + 2
        exact = RayTaskExecution.objects.create(
            task_id="test-detail-exact-byte-boundary",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data=exact_value,
            error_message=exact_value,
        )
        oversized = RayTaskExecution.objects.create(
            task_id="test-detail-over-byte-boundary",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data=oversized_value,
            error_message=oversized_value,
        )

        exact_response = client.get(f"/api/executions/{exact.pk}")

        assert exact_response.status_code == 200
        exact_payload = exact_response.json()
        assert exact_payload["result_data"] == REDACTED
        assert exact_payload["error_message"] == redact_text(exact_value)
        assert exact_payload["result_data_omission_reason"] is None
        assert exact_payload["error_message_omission_reason"] is None

        captured_rows = []
        original_item = testproject_api._execution_detail_item

        def capture_item(row):
            captured_rows.append(dict(row))
            return original_item(row)

        monkeypatch.setattr(testproject_api, "_execution_detail_item", capture_item)
        with CaptureQueriesContext(connection) as queries:
            oversized_response = client.get(f"/api/executions/{oversized.pk}")

        assert oversized_response.status_code == 200
        oversized_payload = oversized_response.json()
        assert oversized_payload["result_data"] is None
        assert oversized_payload["error_message"] is None
        assert (
            oversized_payload["result_data_omission_reason"] == "stored_value_exceeds_detail_limit"
        )
        assert (
            oversized_payload["error_message_omission_reason"]
            == "stored_value_exceeds_detail_limit"
        )
        assert protected_marker not in oversized_response.content.decode()
        assert captured_rows[0]["_detail_result_data"] is None
        assert captured_rows[0]["_detail_error_message"] is None
        assert captured_rows[0]["_detail_result_data_bytes"] == limit + 2
        assert captured_rows[0]["_detail_error_message_bytes"] == limit + 2

        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 1
        guarded_sql = task_selects[0]
        assert "CASE WHEN" in guarded_sql
        assert "LENGTH(CAST(" in guarded_sql
        assert 'THEN "django_ray_raytaskexecution"."result_data"' in guarded_sql
        assert 'THEN "django_ray_raytaskexecution"."error_message"' in guarded_sql

    def test_execution_detail_omits_result_first_at_aggregate_response_limit(
        self,
        client,
    ):
        limit = testproject_api._EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
        diagnostic = "\\" * limit
        serialized_result = json.dumps("\\" * ((limit - 2) // 2))
        assert len(serialized_result.encode()) == limit
        task = RayTaskExecution.objects.create(
            task_id="test-detail-aggregate-response-boundary",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data=serialized_result,
            error_message=diagnostic,
        )

        response = client.get(f"/api/executions/{task.pk}")
        repeated = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == repeated.status_code == 200
        assert response.content == repeated.content
        assert len(response.content) <= testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES
        payload = response.json()
        assert payload["result_data"] is None
        assert payload["result_data_omission_reason"] == "response_size_limit"
        assert payload["error_message"] == diagnostic
        assert payload["error_message_omission_reason"] is None

    def test_execution_detail_returns_fixed_bounded_failure_when_metadata_cannot_fit(
        self,
        client,
        monkeypatch,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-detail-fixed-response-limit-failure",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data="stored-result-must-not-leak",
            error_message="stored-error-must-not-leak",
        )

        monkeypatch.setattr(
            testproject_api,
            "_try_encode_execution_detail_response",
            lambda request, response: (
                b"x" * (testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES + 1)
            ),
        )

        response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 503
        assert len(response.content) <= testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES
        assert response.json() == {
            "code": "execution_detail_response_limit",
            "message": "Execution detail exceeds its fixed response limit.",
            "response_max_bytes": testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES,
        }
        assert b"stored-result" not in response.content
        assert b"stored-error" not in response.content
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.parametrize(
        ("failure_mode", "expected_statuses"),
        (
            ("initial", [200, 503]),
            ("fallback", [200, 200, 503]),
            ("fixed", [200, 503]),
        ),
    )
    def test_execution_detail_renderer_exceptions_return_fixed_bounded_failure(
        self,
        client,
        monkeypatch,
        failure_mode,
        expected_statuses,
    ):
        task = RayTaskExecution.objects.create(
            task_id=f"test-detail-renderer-{failure_mode}",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data="stored-renderer-result-must-not-leak",
            error_message="stored-renderer-error-must-not-leak",
        )
        original_render = testproject_api.api.renderer.render
        statuses = []

        def render(request, data, *, response_status):
            statuses.append(response_status)
            if failure_mode == "fallback" and len(statuses) == 1:
                return b"x" * (testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES + 1)
            if failure_mode == "fixed" or response_status == 200:
                raise RuntimeError("custom renderer failed")
            return original_render(request, data, response_status=response_status)

        monkeypatch.setattr(testproject_api.api.renderer, "render", render)

        response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 503
        assert statuses == expected_statuses
        assert response.json() == {
            "code": "execution_detail_response_limit",
            "message": "Execution detail exceeds its fixed response limit.",
            "response_max_bytes": testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES,
        }
        assert len(response.content) <= testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES
        assert b"stored-renderer-result" not in response.content
        assert b"stored-renderer-error" not in response.content
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.parametrize("exception_type", (SystemExit, KeyboardInterrupt))
    def test_execution_detail_renderer_does_not_swallow_process_control(
        self,
        client,
        monkeypatch,
        exception_type,
    ):
        task = RayTaskExecution.objects.create(
            task_id=f"test-detail-renderer-{exception_type.__name__}",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data="5",
        )

        def render(request, data, *, response_status):
            raise exception_type("process control")

        monkeypatch.setattr(testproject_api.api.renderer, "render", render)

        with pytest.raises(exception_type, match="process control"):
            client.get(f"/api/executions/{task.pk}")

    @pytest.mark.parametrize("exception_type", (SystemExit, KeyboardInterrupt))
    def test_execution_detail_fixed_renderer_does_not_swallow_process_control(
        self,
        monkeypatch,
        exception_type,
    ):
        def render(request, data, *, response_status):
            raise exception_type("process control")

        monkeypatch.setattr(testproject_api.api.renderer, "render", render)

        with pytest.raises(exception_type, match="process control"):
            testproject_api._execution_detail_unavailable_response(object())

    def test_execution_detail_decodes_valid_json_escapes_before_redaction(
        self,
        client,
        settings,
    ):
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"password"]}
        task = RayTaskExecution.objects.create(
            task_id="test-detail-valid-escaped-key",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=r'{"\u0070assword":"CANARY_VALID_ESCAPED_KEY"}',
        )

        detail = client.get(f"/api/executions/{task.pk}")
        listing = client.get("/api/executions", {"task_id": task.task_id})

        assert detail.status_code == listing.status_code == 200
        detail_result = detail.json()["result_data"]
        list_result = listing.json()["tasks"][0]["result_data"]
        assert json.loads(detail_result) == {"<redacted>": REDACTED}
        assert json.loads(list_result) == {"<redacted>": REDACTED}
        assert "CANARY_VALID_ESCAPED_KEY" not in detail.content.decode()
        assert "CANARY_VALID_ESCAPED_KEY" not in listing.content.decode()
        assert detail.json()["result_data_omission_reason"] is None
        assert listing.json()["tasks"][0]["result_data_omission_reason"] is None

    @pytest.mark.parametrize(
        ("case_name", "result_data"),
        (
            (
                "escaped-key",
                r'{"\u0070assword":"CANARY_ESCAPED_KEY",',
            ),
            (
                "escaped-value",
                r'{"safe":"\u0043ANARY_ESCAPED_VALUE",',
            ),
            (
                "mixed-escapes",
                r'{"pass\u0077ord":"CAN\u0041RY_MIXED",',
            ),
            (
                "unicode-key",
                r'{"p\u00e4ssword":"CANARY_UNICODE_KEY",',
            ),
            (
                "truncated-escape",
                r'{"safe":"CANARY_TRUNCATED\u00',
            ),
            (
                "embedded-control",
                '{"safe":"CANARY_CONTROL\x00"',
            ),
        ),
    )
    def test_execution_detail_and_list_malformed_json_fail_closed(
        self,
        client,
        settings,
        case_name,
        result_data,
    ):
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"password", r"pässword"]}
        task = RayTaskExecution.objects.create(
            task_id=f"test-detail-malformed-{case_name}",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=result_data,
        )

        detail = client.get(f"/api/executions/{task.pk}")
        listing = client.get("/api/executions", {"task_id": task.task_id})

        assert detail.status_code == listing.status_code == 200
        assert detail.json()["result_data"] == REDACTED
        assert listing.json()["tasks"][0]["result_data"] == REDACTED
        assert detail.json()["result_data_omission_reason"] is None
        assert listing.json()["tasks"][0]["result_data_omission_reason"] is None
        assert "CANARY" not in detail.content.decode()
        assert "CANARY" not in listing.content.decode()

    def test_execution_detail_deep_json_conversion_fails_closed(
        self,
        client,
    ):
        result_data = "[" * 10_000 + '"CANARY_DEEP"' + "]" * 10_000
        task = RayTaskExecution.objects.create(
            task_id="test-detail-deep-json-conversion",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=result_data,
        )

        response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 200
        assert response.json()["result_data"] == REDACTED
        assert response.json()["result_data_omission_reason"] is None
        assert "CANARY_DEEP" not in response.content.decode()

    def test_execution_detail_and_list_json_depth_boundary_is_stable(
        self,
        client,
    ):
        accepted_value = "[" * 20 + '"visible-depth-boundary"' + "]" * 20
        rejected_value = "[" * 21 + '"CANARY_OVER_DEPTH"' + "]" * 21
        accepted = RayTaskExecution.objects.create(
            task_id="test-detail-json-depth-accepted",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=accepted_value,
        )
        rejected = RayTaskExecution.objects.create(
            task_id="test-detail-json-depth-rejected",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=rejected_value,
        )

        accepted_detail = client.get(f"/api/executions/{accepted.pk}")
        accepted_list = client.get("/api/executions", {"task_id": accepted.task_id})
        rejected_detail = client.get(f"/api/executions/{rejected.pk}")
        rejected_list = client.get("/api/executions", {"task_id": rejected.task_id})

        assert accepted_detail.status_code == accepted_list.status_code == 200
        assert rejected_detail.status_code == rejected_list.status_code == 200
        assert json.loads(accepted_detail.json()["result_data"]) == json.loads(accepted_value)
        assert json.loads(accepted_list.json()["tasks"][0]["result_data"]) == json.loads(
            accepted_value
        )
        assert rejected_detail.json()["result_data"] == REDACTED
        assert rejected_list.json()["tasks"][0]["result_data"] == REDACTED
        assert "CANARY_OVER_DEPTH" not in rejected_detail.content.decode()
        assert "CANARY_OVER_DEPTH" not in rejected_list.content.decode()

    def test_execution_detail_depth_scan_exhaustion_is_fixed_redaction(
        self,
        client,
    ):
        nested_safe_value = "[" * 21 + '"CANARY_AFTER_SENSITIVE_WIDTH"' + "]" * 21
        result_data = (
            '{"password":'
            + json.dumps([0] * testproject_api._EXECUTION_RESULT_JSON_DEPTH_SCAN_MAX_ITEMS)
            + ',"safe":'
            + nested_safe_value
            + "}"
        )
        task = RayTaskExecution.objects.create(
            task_id="test-detail-json-depth-budget-exhausted",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=result_data,
        )

        response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 200
        assert response.json()["result_data"] == REDACTED
        assert "<max-depth>" not in response.content.decode()
        assert "CANARY_AFTER_SENSITIVE_WIDTH" not in response.content.decode()

    def test_execution_json_depth_preflight_has_fixed_width_work(self):
        traversed_items = 0

        class CountingList(list):
            def __iter__(self):
                nonlocal traversed_items
                for item in super().__iter__():
                    traversed_items += 1
                    yield item

        value = CountingList(
            [None] * (testproject_api._EXECUTION_RESULT_JSON_DEPTH_SCAN_MAX_ITEMS + 100)
        )

        assert testproject_api._json_value_requires_fixed_redaction(value) is True
        assert traversed_items == testproject_api._EXECUTION_RESULT_JSON_DEPTH_SCAN_MAX_ITEMS

    def test_execution_detail_unicode_conversion_failure_is_fixed_redaction(
        self,
        client,
        monkeypatch,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-detail-unicode-conversion-failure",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data='{"safe":"CANARY_UNICODE_CONVERSION"}',
        )

        def reject_conversion(value):
            raise UnicodeError("forced conversion failure")

        monkeypatch.setattr(testproject_api, "safe_json_dumps", reject_conversion)

        detail = client.get(f"/api/executions/{task.pk}")
        listing = client.get("/api/executions", {"task_id": task.task_id})

        assert detail.status_code == listing.status_code == 200
        assert detail.json()["result_data"] == REDACTED
        assert listing.json()["tasks"][0]["result_data"] == REDACTED
        assert detail.json()["result_data_omission_reason"] is None
        assert listing.json()["tasks"][0]["result_data_omission_reason"] is None
        assert "CANARY_UNICODE_CONVERSION" not in detail.content.decode()
        assert "CANARY_UNICODE_CONVERSION" not in listing.content.decode()

    def test_execution_payload_redacts_sensitive_fields(self, client, settings):
        """Operator-facing execution details must not echo stored secrets."""
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"password"]}
        task = RayTaskExecution.objects.create(
            task_id="test-redacted-api",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
            args_json='[{"password":"api-secret"}]',
            kwargs_json='{"safe":"visible"}',
            result_data='{"password":"result-secret","safe":1}',
            progress_data='{"safe":"visible"}',
            error_message="pass\x1b[31mword=error-secret",
        )

        response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 200
        payload = response.json()
        serialized = str(payload)
        assert "api-secret" not in serialized
        assert "result-secret" not in serialized
        assert "error-secret" not in serialized
        assert "[REDACTED]" in serialized
        assert "progress_data" not in payload

    def test_execution_monitoring_reads_defer_complete_progress_payloads(self, client):
        task = RayTaskExecution.objects.create(
            task_id="test-bounded-execution-api",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            progress_data="legacy-graph" * 10_000,
            workflow_progress_summary_json="summary" * 10_000,
        )

        with CaptureQueriesContext(connection) as queries:
            detail = client.get(f"/api/executions/{task.pk}")
            listing = client.get("/api/executions")

        assert detail.status_code == 200
        assert listing.status_code == 200
        assert "progress_data" not in detail.json()
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert task_selects
        assert all("progress_data" not in query for query in task_selects)
        assert all("workflow_progress_summary_json" not in query for query in task_selects)

    def test_get_execution_not_found(self, client):
        """Test getting a non-existent execution."""
        response = client.get("/api/executions/99999")
        assert response.status_code == 404

    def test_get_execution_requires_authentication_before_database_access(
        self,
        unauthenticated_client,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-detail-auth-before-query",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )

        with CaptureQueriesContext(connection) as queries:
            response = unauthenticated_client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 401
        assert not [
            query
            for query in queries.captured_queries
            if "django_ray_raytaskexecution" in query["sql"]
        ]

    def test_delete_execution_is_not_a_supported_lifecycle_operation(self, client):
        """A REST client cannot erase the durable row behind active Ray work."""
        task = RayTaskExecution.objects.create(
            task_id="test-delete",
            callable_path="test.task",
            state=TaskState.RUNNING,
        )

        response = client.delete(f"/api/executions/{task.pk}")
        assert response.status_code == 405
        assert RayTaskExecution.objects.filter(pk=task.pk, state=TaskState.RUNNING).exists()

    def test_openapi_does_not_advertise_execution_deletion(self, client):
        """The reusable sample advertises only bounded lifecycle operations."""
        schema_response = client.get("/api/openapi.json")
        assert schema_response.status_code == 200
        schema = schema_response.json()
        execution_path = schema["paths"]["/api/executions/{execution_id}"]
        assert set(execution_path) == {"get"}
        responses = execution_path["get"]["responses"]
        assert responses["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/TaskExecutionDetailSchema"
        }
        assert responses["503"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/TaskExecutionDetailUnavailableSchema"
        }
        detail_schema = schema["components"]["schemas"]["TaskExecutionDetailSchema"]
        detail_properties = detail_schema["properties"]
        assert detail_properties["diagnostic_max_bytes"]["const"] == (
            testproject_api._EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
        )
        assert detail_properties["response_max_bytes"]["const"] == (
            testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES
        )
        result_reason = next(
            item
            for item in detail_properties["result_data_omission_reason"]["anyOf"]
            if "enum" in item
        )
        assert result_reason["enum"] == [
            "stored_value_exceeds_detail_limit",
            "external_result_not_loaded",
            "response_size_limit",
        ]
        assert "/api/executions/reset" not in schema["paths"]
        assert "/api/cluster/workflows/{task_id}/nodes/{node_id}" not in schema["paths"]
        assert "MessageSchema" not in schema["components"]["schemas"]
        assert "WorkflowNodeSchema" not in schema["components"]["schemas"]
        assert set(schema["paths"]["/api/executions/{execution_id}/retry"]) == {"post"}
        assert set(schema["paths"]["/api/executions/{execution_id}/cancel"]) == {"post"}
        assert set(schema["paths"]["/api/cluster/workflows/{task_id}/node-detail"]) == {"get"}
        status_properties = schema["components"]["schemas"]["TaskStatusSchema"]["properties"]
        assert status_properties["input_max_bytes"]["const"] == (
            testproject_api._TASK_STATUS_INPUT_MAX_BYTES
        )
        assert status_properties["response_max_bytes"]["const"] == (
            testproject_api._TASK_STATUS_RESPONSE_MAX_BYTES
        )
        status_reason = next(
            item for item in status_properties["input_omission_reason"]["anyOf"] if "enum" in item
        )
        assert status_reason["enum"] == [
            "external_input_not_loaded",
            "stored_input_exceeds_status_limit",
            "malformed_inline_input",
            "encoded_response_limit",
        ]
        assert status_properties["queue_capacity_attested"]["const"] is False
        provenance_string = next(
            item
            for item in status_properties["created_with_django_ray_version"]["anyOf"]
            if item.get("type") == "string"
        )
        assert provenance_string["maxLength"] == 128
        assert detail_properties["queue_capacity_attested"]["const"] is False

    def test_cancel_queued_execution(self, client):
        """Queued cancellation returns a small accepted outcome and archives once."""
        task = RayTaskExecution.objects.create(
            task_id="test-cancel",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )

        response = client.post(f"/api/executions/{task.pk}/cancel")
        assert response.status_code == 202
        data = response.json()
        assert data == {
            "code": "ACCEPTED",
            "message": "Cancellation was accepted.",
            "execution_id": task.pk,
            "state": "CANCELLED",
            "attempt_number": 1,
            "execution_generation": 0,
            "next_action": "The queued attempt is cancelled; retain its archived history.",
            "response_max_bytes": testproject_api._CANCELLATION_RESPONSE_MAX_BYTES,
        }
        assert len(response.content) <= testproject_api._CANCELLATION_RESPONSE_MAX_BYTES
        assert response["Cache-Control"] == "no-store"
        assert response["X-Content-Type-Options"] == "nosniff"
        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.finished_at is not None
        assert TaskAttempt.objects.get(execution=task).state == TaskState.CANCELLED

    def test_cancel_running_execution_requests_worker_cancellation(self, client):
        task = RayTaskExecution.objects.create(
            task_id="test-cancel-running",
            callable_path="test.task",
            state=TaskState.RUNNING,
            execution_generation=4,
        )

        response = client.post(f"/api/executions/{task.pk}/cancel")

        assert response.status_code == 202
        data = response.json()
        assert data["code"] == "ACCEPTED"
        assert data["state"] == "CANCELLING"
        assert data["execution_generation"] == 4
        assert data["next_action"] == (
            "Poll until the worker records a terminal cancellation outcome."
        )
        assert "finished_at" not in data
        assert "cancellation_error" not in data

    @pytest.mark.django_db(transaction=True)
    def test_cancel_unsupported_protocol_is_an_explicit_conflict_without_mutation(
        self,
        client,
    ) -> None:
        close_legacy_worker_admission(
            expected_revision=1,
            legacy_producers_retired=True,
        )
        task = RayTaskExecution.objects.create(
            task_id="test-cancel-unsupported-protocol",
            callable_path="test.task",
            state=TaskState.QUEUED,
            execution_protocol_version=2,
            attempt_number=3,
            execution_generation=7,
        )
        before = RayTaskExecution.objects.filter(pk=task.pk).values().get()

        response = client.post(f"/api/executions/{task.pk}/cancel")

        assert response.status_code == 409
        assert response.json() == {
            "code": "UNSUPPORTED_PROTOCOL",
            "message": "This django-ray build does not support the execution protocol.",
            "execution_id": task.pk,
            "state": "QUEUED",
            "attempt_number": 3,
            "execution_generation": 7,
            "next_action": (
                "Route this execution to a django-ray build that supports its protocol "
                "before cancelling."
            ),
            "response_max_bytes": testproject_api._CANCELLATION_RESPONSE_MAX_BYTES,
        }
        assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_cancel_terminal_execution_is_an_explicit_conflict(self, client):
        task = RayTaskExecution.objects.create(
            task_id="test-cancel-terminal",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            execution_generation=2,
        )

        first = client.post(f"/api/executions/{task.pk}/cancel")
        second = client.post(f"/api/executions/{task.pk}/cancel")

        assert first.status_code == 409
        assert second.status_code == 409
        assert first.json()["code"] == "ALREADY_TERMINAL"
        assert second.json()["code"] == "ALREADY_TERMINAL"
        assert first.json()["state"] == "SUCCEEDED"
        assert second.json()["state"] == "SUCCEEDED"

    def test_cancel_terminal_execution_never_selects_or_returns_diagnostics(self, client):
        protected = "cancel-response-protected-marker"
        task = RayTaskExecution.objects.create(
            task_id="test-cancel-terminal-wide-result",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=json.dumps({"protected": protected}),
            result_reference=f"digest:{protected}",
            error_message=protected,
            error_traceback=protected,
            cancellation_error=protected,
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.post(f"/api/executions/{task.pk}/cancel")

        assert response.status_code == 409
        assert response.json()["code"] == "ALREADY_TERMINAL"
        assert response.json()["state"] == "SUCCEEDED"
        assert protected not in response.content.decode()
        assert set(response.json()) == {
            "code",
            "message",
            "execution_id",
            "state",
            "attempt_number",
            "execution_generation",
            "next_action",
            "response_max_bytes",
        }
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 2
        for sql in task_selects:
            for excluded in (
                "result_data",
                "result_reference",
                "error_message",
                "error_traceback",
                "cancellation_error",
                "runtime_env_json",
                "progress_data",
            ):
                assert excluded not in sql

    def test_cancel_duplicate_missing_and_post_preflight_race_are_distinct(
        self,
        client,
        monkeypatch,
    ):
        duplicate = RayTaskExecution.objects.create(
            task_id="test-cancel-duplicate",
            callable_path="test.task",
            state=TaskState.CANCELLING,
            attempt_number=2,
            execution_generation=3,
        )

        duplicate_response = client.post(f"/api/executions/{duplicate.pk}/cancel")
        missing_response = client.post("/api/executions/999999/cancel")

        assert duplicate_response.status_code == 409
        assert duplicate_response.json()["code"] == "ALREADY_REQUESTED"
        assert missing_response.status_code == 404
        assert missing_response.json()["code"] == "NOT_FOUND"

        observed: dict[str, int] = {}

        def raced(execution_id, *, expected_attempt_number, expected_execution_generation):
            observed.update(
                execution_id=execution_id,
                attempt_number=expected_attempt_number,
                execution_generation=expected_execution_generation,
            )
            return TaskCancellationRequestResult(
                status=TaskCancellationRequestStatus.STALE_ATTEMPT,
                execution_id=execution_id,
                state=TaskState.RUNNING,
                attempt_number=3,
                execution_generation=4,
            )

        monkeypatch.setattr(testproject_api, "request_task_cancellation", raced)
        raced_response = client.post(f"/api/executions/{duplicate.pk}/cancel")

        assert raced_response.status_code == 409
        assert raced_response.json()["code"] == "STALE_ATTEMPT"
        assert observed == {
            "execution_id": duplicate.pk,
            "attempt_number": 2,
            "execution_generation": 3,
        }

    def test_retry_failed_execution(self, client):
        """Test retrying a failed execution."""
        task = RayTaskExecution.objects.create(
            task_id="test-retry",
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            callable_path="test.task",
            state=TaskState.FAILED,
            error_message="Some error",
            attempt_number=1,
        )

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            response = client.post(f"/api/executions/{task.pk}/retry")
        assert response.status_code == 202
        data = response.json()
        assert data["code"] == "ACCEPTED"
        assert data["message"] == "A new task attempt was queued."
        assert data["execution_id"] == task.pk
        assert data["state"] == "QUEUED"
        assert data["attempt_number"] == 2
        assert data["execution_generation"] == 1
        assert data["next_action"] == "Poll or inspect the newly queued attempt."
        assert "result_data" not in data
        assert "error_message" not in data
        assert TaskAttempt.objects.get(execution=task).state == TaskState.FAILED

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            duplicate = client.post(f"/api/executions/{task.pk}/retry")
        assert duplicate.status_code == 409
        duplicate_data = duplicate.json()
        assert duplicate_data["code"] == "NOT_RETRYABLE"
        assert duplicate_data["state"] == "QUEUED"
        assert duplicate_data["attempt_number"] == 2
        assert duplicate_data["execution_generation"] == 1
        assert "FAILED, CANCELLED, LOST, or EXPIRED" in duplicate_data["next_action"]
        assert TaskAttempt.objects.filter(execution=task).count() == 1

    def test_retry_unsupported_protocol_is_an_explicit_conflict_without_mutation(
        self,
        client,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="test-retry-unsupported-protocol",
            callable_path="test.task",
            state=TaskState.FAILED,
            execution_protocol_version=2,
            attempt_number=3,
            execution_generation=7,
            error_message="retained failure",
        )
        before = RayTaskExecution.objects.filter(pk=task.pk).values().get()

        response = client.post(f"/api/executions/{task.pk}/retry")

        assert response.status_code == 409
        assert response.json() == {
            "code": "UNSUPPORTED_PROTOCOL",
            "message": "This django-ray build does not support the execution protocol.",
            "execution_id": task.pk,
            "state": "FAILED",
            "attempt_number": 3,
            "execution_generation": 7,
            "next_action": (
                "Route this execution to a django-ray build that supports its protocol "
                "before retrying."
            ),
        }
        assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_retry_succeeded_execution_guides_a_fresh_enqueue_without_mutation(self, client):
        result_marker = "successful-result-must-not-leak-issue-321"
        reference_marker = "successful-reference-must-not-leak-issue-321"
        task = RayTaskExecution.objects.create(
            task_id="test-retry-succeeded-conflict",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            attempt_number=3,
            execution_generation=3,
            workflow_run_id="00000000-0000-0000-0000-000000000321",
            result_data=json.dumps({"marker": result_marker}),
            result_reference=f"digest:{reference_marker}",
            finished_at=datetime(2026, 8, 2, tzinfo=UTC),
        )
        before = RayTaskExecution.objects.filter(pk=task.pk).values().get()

        response = client.post(f"/api/executions/{task.pk}/retry")

        assert response.status_code == 409
        data = response.json()
        assert data == {
            "code": "NOT_RETRYABLE",
            "message": "The execution is not retryable from its current state.",
            "execution_id": task.pk,
            "state": "SUCCEEDED",
            "attempt_number": 3,
            "execution_generation": 3,
            "next_action": (
                "Enqueue a new task under the application's authorization and idempotency "
                "policy; keep this successful execution as completed history."
            ),
        }
        serialized = response.content.decode()
        assert result_marker not in serialized
        assert reference_marker not in serialized
        assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_retry_raced_identity_returns_only_bounded_current_identity(
        self,
        client,
        monkeypatch,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-retry-raced-identity",
            callable_path="test.task",
            state=TaskState.FAILED,
            attempt_number=2,
            execution_generation=4,
            workflow_run_id="00000000-0000-0000-0000-000000000322",
            workflow_plan_fingerprint="sha256:" + ("a" * 64),
            result_data='{"marker":"raced-result-must-not-leak"}',
            error_message="raced-error-must-not-leak",
        )

        def raced_retry(execution_id, **kwargs):
            assert execution_id == task.pk
            assert kwargs["expected_attempt_number"] == 2
            assert kwargs["expected_execution_generation"] == 4
            assert kwargs["expected_workflow_identity"] == (
                "00000000-0000-0000-0000-000000000322",
                "sha256:" + ("a" * 64),
            )
            return TaskRetryRequestResult(
                status=TaskRetryRequestStatus.STALE_ATTEMPT,
                execution_id=task.pk,
                state=TaskState.QUEUED,
                attempt_number=3,
                execution_generation=5,
            )

        monkeypatch.setattr("testproject.api.request_task_retry", raced_retry)

        response = client.post(f"/api/executions/{task.pk}/retry")

        assert response.status_code == 409
        assert response.json() == {
            "code": "STALE_ATTEMPT",
            "message": "The execution attempt changed before the retry could be applied.",
            "execution_id": task.pk,
            "state": "QUEUED",
            "attempt_number": 3,
            "execution_generation": 5,
            "next_action": (
                "Refresh and re-authorize the current attempt before deciding whether to retry."
            ),
        }
        serialized = response.content.decode()
        assert "raced-result-must-not-leak" not in serialized
        assert "raced-error-must-not-leak" not in serialized

    def test_retry_missing_execution_returns_a_structured_not_found(self, client):
        response = client.post("/api/executions/99999/retry")

        assert response.status_code == 404
        assert response.json() == {
            "code": "NOT_FOUND",
            "message": "The execution was not found.",
            "execution_id": 99999,
            "state": None,
            "attempt_number": None,
            "execution_generation": None,
            "next_action": "Verify the execution identifier and object authorization.",
        }

    def test_retry_raced_deletion_returns_structured_not_found(
        self,
        client,
        monkeypatch,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-retry-raced-deletion",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data='{"marker":"deleted-race-result-must-not-leak"}',
            error_message="deleted-race-error-must-not-leak",
        )

        def raced_retry(execution_id, **_kwargs):
            assert execution_id == task.pk
            return TaskRetryRequestResult(
                status=TaskRetryRequestStatus.NOT_FOUND,
                execution_id=task.pk,
                state=None,
                attempt_number=None,
                execution_generation=None,
            )

        monkeypatch.setattr("testproject.api.request_task_retry", raced_retry)

        response = client.post(f"/api/executions/{task.pk}/retry")

        assert response.status_code == 404
        assert response.json() == {
            "code": "NOT_FOUND",
            "message": "The execution was not found.",
            "execution_id": task.pk,
            "state": None,
            "attempt_number": None,
            "execution_generation": None,
            "next_action": "Verify the execution identifier and object authorization.",
        }
        serialized = response.content.decode()
        assert "deleted-race-result-must-not-leak" not in serialized
        assert "deleted-race-error-must-not-leak" not in serialized

    def test_retry_execution_requires_bearer_authorization(
        self,
        unauthenticated_client,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-retry-unauthorized",
            callable_path="test.task",
            state=TaskState.FAILED,
            error_message="authorization must run before retry",
        )

        response = unauthenticated_client.post(f"/api/executions/{task.pk}/retry")

        assert response.status_code == 401
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 1
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_retry_rejects_corrupt_runtime_env_without_disclosure(self, client):
        task = RayTaskExecution.objects.create(
            task_id="test-retry-runtime-env-corrupt",
            callable_path="test.task",
            state=TaskState.FAILED,
            error_message="original failure",
            attempt_number=2,
            execution_generation=4,
            runtime_env_json=('{"env_vars":{"VALUE":"arbitrary-customer-marker-7cf3"}}'),
            runtime_env_hash="0" * 64,
        )

        response = client.post(f"/api/executions/{task.pk}/retry")

        assert response.status_code == 409
        assert response.json() == {"detail": "Persisted RuntimeEnv snapshot failed validation"}
        assert "arbitrary-customer-marker-7cf3" not in response.content.decode()
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 2
        assert task.execution_generation == 4
        assert task.error_message == "original failure"
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_bulk_reset_route_is_removed_without_mutating_retryable_rows(self, client):
        failed = RayTaskExecution.objects.create(
            task_id="test-retired-bulk-reset",
            callable_path="test.task",
            state=TaskState.FAILED,
            progress_data='{"revision":4}',
            workflow_run_id="00000000-0000-0000-0000-000000000126",
            error_message="retryable",
            attempt_number=2,
            execution_generation=4,
        )
        terminal = serialize_workflow_progress_summary(
            workflow_progress_summary(failed, state="FAILED")
        )
        failed.workflow_progress_summary_json = terminal
        failed.save(update_fields=["workflow_progress_summary_json"])
        before = RayTaskExecution.objects.filter(pk=failed.pk).values().get()

        response = client.post("/api/executions/reset")

        assert response.status_code in {404, 405}
        assert RayTaskExecution.objects.filter(pk=failed.pk).values().get() == before
        assert not TaskAttempt.objects.filter(execution=failed).exists()
        schema = client.get("/api/openapi.json").json()
        assert "/api/executions/reset" not in schema["paths"]
        assert set(schema["paths"]["/api/executions/{execution_id}/retry"]) == {"post"}

    def test_get_stats(self, client):
        """Test getting execution statistics."""
        RayTaskExecution.objects.create(
            task_id="test-1", callable_path="test", state=TaskState.QUEUED
        )
        RayTaskExecution.objects.create(
            task_id="test-2", callable_path="test", state=TaskState.SUCCEEDED
        )
        RayTaskExecution.objects.create(
            task_id="test-3", callable_path="test", state=TaskState.FAILED
        )
        RayTaskExecution.objects.create(
            task_id="test-4", callable_path="test", state=TaskState.EXPIRED
        )

        response = client.get("/api/executions/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 4
        assert data["queued"] == 1
        assert data["succeeded"] == 1
        assert data["failed"] == 1
        assert data["expired"] == 1

    def test_get_stats_uses_single_grouped_query(self, client):
        """Stats endpoint should aggregate states in one query."""
        RayTaskExecution.objects.create(
            task_id="stats-query-test-1",
            callable_path="test",
            state=TaskState.QUEUED,
        )
        RayTaskExecution.objects.create(
            task_id="stats-query-test-2",
            callable_path="test",
            state=TaskState.SUCCEEDED,
        )
        RayTaskExecution.objects.create(
            task_id="stats-query-test-3",
            callable_path="test",
            state=TaskState.FAILED,
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/executions/stats")

        assert response.status_code == 200
        assert len(queries) == 1
