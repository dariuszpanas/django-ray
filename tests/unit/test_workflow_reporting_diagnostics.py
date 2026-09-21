"""Terminal cost records are bounded, fenced and atomic with publication."""

import json
from dataclasses import replace

import pytest

from django_ray.models import WorkflowProgressRunStorage
from django_ray.workflow.progress.publication import publish_terminal_workflow_progress
from django_ray.workflow.progress.reporting_diagnostics import (
    REPORTING_DIAGNOSTICS_MAX_BYTES,
    load_reporting_diagnostics,
    read_reporting_diagnostics,
    serialize_reporting_diagnostics,
)
from tests.unit.test_workflow_progress_publication import _execution, _identity, _snapshot


def test_diagnostics_keep_counters_without_graph_or_values():
    identity = _identity()
    snapshot = _snapshot(identity)
    serialized = serialize_reporting_diagnostics(
        identity,
        detail_revision=1,
        snapshot_revision=snapshot["revision"],
        ingress=snapshot["ingress"],
    )
    record = read_reporting_diagnostics(serialized, identity=identity, detail_revision=1)
    assert record["ingress"] == snapshot["ingress"]
    assert "graph" not in record
    assert len(serialized) <= REPORTING_DIAGNOSTICS_MAX_BYTES
    for wrong_identity in (
        replace(identity, attempt_number=2),
        replace(identity, execution_generation=2),
        replace(identity, task_execution_pk=identity.task_execution_pk + 1),
    ):
        with pytest.raises(ValueError):
            read_reporting_diagnostics(serialized, identity=wrong_identity, detail_revision=1)
    with pytest.raises(ValueError):
        read_reporting_diagnostics(serialized, identity=identity, detail_revision=2)


@pytest.mark.parametrize("damage", ["extra", "secret_counter", "bool", "noncanonical", "large"])
def test_diagnostics_reject_corrupt_records_without_exporting_values(damage):
    identity = _identity()
    snapshot = _snapshot(identity)
    serialized = serialize_reporting_diagnostics(
        identity,
        detail_revision=1,
        snapshot_revision=snapshot["revision"],
        ingress=snapshot["ingress"],
    )
    value = json.loads(serialized)
    if damage == "extra":
        value["private"] = "secret"
    elif damage == "secret_counter":
        value["ingress"]["retained_nodes"] = "secret"
    elif damage == "bool":
        value["run_identity"]["attempt_number"] = True
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if damage == "noncanonical":
        serialized += " "
    if damage == "large":
        serialized += " " * REPORTING_DIAGNOSTICS_MAX_BYTES
    with pytest.raises(ValueError) as error:
        read_reporting_diagnostics(serialized, identity=identity, detail_revision=1)
    assert "secret" not in str(error.value)


@pytest.mark.django_db
def test_terminal_publication_retains_exact_run_diagnostics_without_legacy_snapshot():
    execution, identity = _execution()
    snapshot = _snapshot(identity)
    result = publish_terminal_workflow_progress(identity, snapshot, detail_days=7)
    assert result.accepted
    run = WorkflowProgressRunStorage.objects.get(execution=execution)
    record = load_reporting_diagnostics(identity, detail_revision=run.detail_revision)
    assert record["ingress"] == snapshot["ingress"]
    execution.refresh_from_db()
    assert not execution.progress_data
    WorkflowProgressRunStorage.objects.filter(pk=run.pk).update(reporting_diagnostics_json=None)
    with pytest.raises(ValueError):
        load_reporting_diagnostics(identity, detail_revision=run.detail_revision)


@pytest.mark.django_db
def test_stale_publication_cannot_create_diagnostics():
    execution, identity = _execution()
    type(execution).objects.filter(pk=execution.pk).update(execution_generation=2)
    result = publish_terminal_workflow_progress(identity, _snapshot(identity), detail_days=7)
    assert not result.accepted
    assert not WorkflowProgressRunStorage.objects.filter(
        execution=execution, reporting_diagnostics_json__isnull=False
    ).exists()


@pytest.mark.django_db
def test_diagnostics_failure_rolls_back_detail_and_summary(monkeypatch):
    from django_ray.models import WorkflowProgressNodeDetail
    from django_ray.workflow.progress import reporting_diagnostics

    execution, identity = _execution()

    def refuse(*args, **kwargs):
        raise ValueError("invalid counters")

    monkeypatch.setattr(reporting_diagnostics, "serialize_reporting_diagnostics", refuse)
    result = publish_terminal_workflow_progress(identity, _snapshot(identity), detail_days=7)
    assert not result.accepted
    execution.refresh_from_db()
    assert execution.workflow_progress_summary_json is None
    assert not WorkflowProgressNodeDetail.objects.filter(run_storage__execution=execution).exists()
    assert not WorkflowProgressRunStorage.objects.filter(
        execution=execution, reporting_diagnostics_json__isnull=False
    ).exists()
