"""Migration compatibility for normalized workflow-progress detail storage."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import RESTRICT


@pytest.mark.django_db(transaction=True)
def test_detail_storage_tables_are_additive_reversible_and_rolling_safe() -> None:
    migrate_from = [("django_ray", "0012_workflow_progress_summary")]
    migrate_to = [("django_ray", "0013_workflow_progress_detail_storage")]
    latest = [("django_ray", "0014_raytaskexecution_ray_target_address")]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    try:
        old_apps = executor.loader.project_state(migrate_from).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        execution = old_execution.objects.create(
            task_id="workflow-detail-before-migration",
            callable_path="testproject.tasks.add_numbers",
            state="RUNNING",
            attempt_number=2,
            execution_generation=3,
            workflow_run_id="00000000-0000-0000-0000-000000000126",
            progress_data='{"schema_version":2}',
            workflow_progress_summary_json='{"schema_version":3}',
            created_at=datetime.now(UTC),
        )

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        run_model = new_apps.get_model("django_ray", "WorkflowProgressRunStorage")
        manifest_model = new_apps.get_model("django_ray", "WorkflowProgressTopologyManifest")
        page_model = new_apps.get_model("django_ray", "WorkflowProgressTopologyPage")
        link_model = new_apps.get_model("django_ray", "WorkflowProgressTopologyManifestPage")
        detail_model = new_apps.get_model("django_ray", "WorkflowProgressNodeDetail")
        assert run_model._meta.get_field("detail_retention_days").default == 7

        migrated = new_execution.objects.get(pk=execution.pk)
        assert migrated.progress_data == '{"schema_version":2}'
        assert migrated.workflow_progress_summary_json == '{"schema_version":3}'
        assert run_model.objects.count() == 0
        assert manifest_model.objects.count() == 0
        assert page_model.objects.count() == 0
        assert link_model.objects.count() == 0
        assert detail_model.objects.count() == 0

        # A process still using the 0012 model can insert because 0013 only adds tables.
        rolling = old_execution.objects.create(
            task_id="workflow-detail-rolling-writer",
            callable_path="testproject.tasks.add_numbers",
            created_at=datetime.now(UTC),
        )
        assert new_execution.objects.filter(pk=rolling.pk).exists()

        detail_payload = b'{"node_id":"node-a","state":"RUNNING"}'
        topology_payload = b'{"items":[{"node_id":"node-a"}]}'
        run_storage = run_model.objects.create(
            execution=migrated,
            attempt_number=2,
            execution_generation=3,
            run_id="00000000-0000-0000-0000-000000000126",
            detail_revision=1,
            detail_node_count=1,
            detail_pending_count=0,
            detail_running_count=1,
            detail_succeeded_count=0,
            detail_failed_count=0,
            detail_truncated_count=1,
            detail_event_count=1,
            detail_truncation_reasons="record_size_limit,reporting_policy",
            detail_encoded_bytes=len(detail_payload),
            detail_decoded_bytes=len(detail_payload),
            detail_retention_days=0,
        )
        manifest = manifest_model.objects.create(
            run_storage=run_storage,
            topology_version=1,
            slot="CURRENT",
            manifest_digest="a" * 64,
            truncation_reasons="edge_count_limit,node_count_limit",
            payload=b'{"pages":[]}',
            node_count=1,
            edge_count=0,
            node_page_count=1,
            edge_page_count=0,
            encoded_bytes=len(topology_payload),
            decoded_bytes=len(topology_payload),
            published_at=datetime.now(UTC),
        )
        page = page_model.objects.create(
            run_storage=run_storage,
            digest=hashlib.sha256(topology_payload).hexdigest(),
            collection="NODE",
            encoding="identity",
            payload=topology_payload,
            item_count=1,
            encoded_bytes=len(topology_payload),
            decoded_bytes=len(topology_payload),
        )
        link_model.objects.create(
            manifest=manifest,
            page=page,
            collection="NODE",
            page_index=0,
        )
        detail_model.objects.create(
            run_storage=run_storage,
            node_key=hashlib.sha256(b"node-a").hexdigest(),
            node_id="node-a",
            invocation_id="00000000-0000-0000-0000-000000000127",
            state="RUNNING",
            event_count=1,
            truncated=True,
            payload=detail_payload,
            digest=hashlib.sha256(detail_payload).hexdigest(),
            encoded_bytes=len(detail_payload),
            decoded_bytes=len(detail_payload),
            last_topology_version=1,
            last_detail_revision=1,
        )

        assert page_model._meta.get_field("manifest_links").related_model is link_model
        assert run_storage.detail_event_count == 1
        assert run_storage.detail_running_count == 1
        assert run_storage.detail_truncated_count == 1
        assert run_storage.detail_truncation_reasons == "record_size_limit,reporting_policy"
        assert run_storage.detail_retention_days == 0
        assert manifest.truncation_reasons == "edge_count_limit,node_count_limit"
        stored_detail = detail_model.objects.get(run_storage=run_storage)
        assert stored_detail.event_count == 1
        assert stored_detail.truncated is True
        assert {value for value, _label in detail_model._meta.get_field("state").choices} == {
            "PENDING",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
        }
        assert link_model._meta.get_field("page").remote_field.on_delete is RESTRICT
        assert {index.name for index in run_model._meta.indexes} == {"ray_wf_run_expiry_idx"}
        assert {constraint.name for constraint in run_model._meta.constraints} >= {
            "ray_wf_run_event_count_cap",
            "ray_wf_run_detail_totals",
            "ray_wf_run_state_counts_sum",
            "ray_wf_run_truncated_cap",
            "ray_wf_run_retention_days",
        }
        assert {constraint.name for constraint in manifest_model._meta.constraints} >= {
            "ray_wf_manifest_ver_uniq",
            "ray_wf_manifest_slot_uniq",
            "ray_wf_manifest_slot_state",
        }
        assert {constraint.name for constraint in detail_model._meta.constraints} >= {
            "ray_wf_node_key_uniq",
            "ray_wf_node_event_count_cap",
            "ray_wf_node_identity_size",
        }
        assert {index.name for index in detail_model._meta.indexes} == {
            "ray_wf_node_state_idx",
            "ray_wf_node_event_idx",
        }

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        reverted_apps = executor.loader.project_state(migrate_from).apps
        reverted_models = {model._meta.model_name for model in reverted_apps.get_models()}
        assert "workflowprogressrunstorage" not in reverted_models
        assert "workflowprogresstopologymanifest" not in reverted_models
        assert "workflowprogresstopologypage" not in reverted_models
        assert "workflowprogresstopologymanifestpage" not in reverted_models
        assert "workflowprogressnodedetail" not in reverted_models

        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        reverted = reverted_execution.objects.get(pk=execution.pk)
        assert reverted.progress_data == '{"schema_version":2}'
        assert reverted.workflow_progress_summary_json == '{"schema_version":3}'
        assert reverted_execution.objects.filter(pk=rolling.pk).exists()
    finally:
        MigrationExecutor(connection).migrate(latest)
