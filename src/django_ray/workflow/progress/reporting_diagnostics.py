"""Bounded terminal reporting costs, separate from public graph summaries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS

REPORTING_DIAGNOSTICS_MAX_BYTES = 16 * 1024
_KEYS = {"schema_version", "run_identity", "detail_revision", "snapshot_revision", "ingress"}


def load_reporting_diagnostics(
    identity: WorkflowRunIdentity, *, detail_revision: int, using: str = "default"
) -> dict[str, Any]:
    """Filter character length before transfer, then require bounded ASCII JSON.

    Valid records use one byte per character. Corrupt non-ASCII rows can transfer
    at most four times that bound before the codec rejects them.
    """
    from django.db.models.functions import Length

    from django_ray.models import WorkflowProgressRunStorage

    serialized = (
        WorkflowProgressRunStorage.objects.using(using)
        .filter(
            execution_id=identity.task_execution_pk,
            attempt_number=identity.attempt_number,
            execution_generation=identity.execution_generation,
            run_id=identity.run_id,
            detail_revision=detail_revision,
        )
        .annotate(diagnostics_size=Length("reporting_diagnostics_json"))
        .filter(diagnostics_size__lte=REPORTING_DIAGNOSTICS_MAX_BYTES)
        .values_list("reporting_diagnostics_json", flat=True)
        .first()
    )
    return read_reporting_diagnostics(
        serialized, identity=identity, detail_revision=detail_revision
    )


def _validate_ingress(ingress: Any, revision: int) -> None:
    from django_ray.workflow.progress.publication import _validate_ingress_envelope

    limits = WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS
    _validate_ingress_envelope(ingress, revision=revision, limits=limits)
    for name, maximum in (
        ("retained_nodes", limits.topology_node_max_items),
        ("retained_edges", limits.topology_edge_max_items),
        ("retained_bytes", limits.combined_max_encoded_bytes),
    ):
        if type(ingress[name]) is not int or not 0 <= ingress[name] <= maximum:
            raise ValueError("Invalid reporting retention counter")


def serialize_reporting_diagnostics(
    identity: WorkflowRunIdentity,
    *,
    detail_revision: int,
    snapshot_revision: int,
    ingress: Mapping[str, Any],
) -> str:
    """Produce canonical ASCII containing only fixed counter fields and the run fence."""
    _validate_ingress(ingress, snapshot_revision)
    value = {
        "schema_version": 1,
        "run_identity": identity.as_dict(),
        "detail_revision": detail_revision,
        "snapshot_revision": snapshot_revision,
        "ingress": ingress,
    }
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    read_reporting_diagnostics(serialized, identity=identity, detail_revision=detail_revision)
    return serialized


def read_reporting_diagnostics(
    serialized: str | None,
    *,
    identity: WorkflowRunIdentity,
    detail_revision: int,
) -> dict[str, Any]:
    """Fail closed on missing, oversized, crossed or inconsistent evidence."""
    if (
        not isinstance(serialized, str)
        or not serialized.isascii()
        or not 0 < len(serialized) <= REPORTING_DIAGNOSTICS_MAX_BYTES
    ):
        raise ValueError("Reporting diagnostics are absent or exceed their byte limit")
    try:
        value = json.loads(serialized)
        maximum = WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS.identity_max_integer
        if (
            not isinstance(value, dict)
            or set(value) != _KEYS
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["run_identity"] != identity.as_dict()
            or json.dumps(value["run_identity"], sort_keys=True)
            != json.dumps(identity.as_dict(), sort_keys=True)
            or type(value["detail_revision"]) is not int
            or not 1 <= value["detail_revision"] <= maximum
            or value["detail_revision"] != detail_revision
            or type(value["snapshot_revision"]) is not int
            or not 0 <= value["snapshot_revision"] <= maximum
        ):
            raise ValueError("Invalid reporting diagnostics identity")
        _validate_ingress(value["ingress"], value["snapshot_revision"])
        if json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) != serialized:
            raise ValueError("Reporting diagnostics are not canonical")
    except (ValueError, TypeError, RecursionError, OverflowError) as error:
        raise ValueError("Invalid reporting diagnostics") from error
    return value
