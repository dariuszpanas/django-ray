"""Bounded collector-pressure proof inside the disposable application fixture.

This probes retained-state admission and terminal preparation, not producer
mailbox admission or publication of a real Django task. The surrounding workload
separately verifies task outcomes, durable API/Admin reads and namespace cleanup.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.progress.protocol import (
    WORKFLOW_PROGRESS_LIMITS_V1,
    WorkflowProgressEventKind,
    prepare_workflow_progress_event,
)

FINGERPRINT = "sha256:" + "a" * 64


def pressure_case(identity: WorkflowRunIdentity, *, mapped: bool, failed: bool):
    """Four fixed scenarios use small budgets to force deterministic pressure."""
    plan = {
        "plan_format": "django-ray.workflow-plan",
        "plan_format_version": 1,
        "fingerprint": FINGERPRINT,
        "definition_name": "qualification:pressure",
        "definition_revision": "sha256:" + "b" * 64,
        "topology_class": "static",
        "node_count": 1,
    }
    occurred_at = datetime(2026, 9, 20, tzinfo=UTC)

    def wire(kind, payload):
        return prepare_workflow_progress_event(
            identity.as_dict(), kind, payload, occurred_at=occurred_at
        )

    kind = WorkflowProgressEventKind
    if mapped:
        events = [
            wire(
                kind.MAP_REGISTERED,
                {"node_id": "leaf", "label": "leaf", "max_concurrency": 1, "max_items": None},
            ),
            wire(
                kind.MAP_PROGRESS,
                {
                    "node_id": "leaf",
                    "label": "leaf",
                    "submitted": 1000,
                    "completed": 999,
                    "input_exhausted": True,
                },
            ),
        ]
    else:
        events = [
            wire(
                kind.NODE_REGISTERED,
                {
                    "node_id": "leaf",
                    "label": "leaf",
                    "callable_path": "qualification.pressure.fixed_leaf",
                    "runtime_env": {"mode": "inherit"},
                    "ray_options": {},
                },
            ),
            wire(
                kind.STARTED,
                {
                    "node_id": "leaf",
                    "label": "leaf",
                    "execution": {
                        "assigned_resources": {},
                        "ray_job_id": None,
                        "ray_node_id": None,
                        "ray_task_id": None,
                        "ray_worker_id": None,
                    },
                },
            ),
            wire(
                kind.APPLICATION_PROGRESS,
                {
                    "node_id": "leaf",
                    "current": 1.0,
                    "total": 2.0,
                    "message": "x" * 400,
                    "metrics": {},
                },
            ),
        ]
    terminal = {"node_id": "leaf", "label": "leaf"}
    if failed:
        terminal["error"] = "fixed pressure failure"
    events.append(wire(kind.FAILED if failed else kind.COMPLETED, terminal))
    return (
        replace(WORKFLOW_PROGRESS_LIMITS_V1, combined_max_decoded_bytes=1300 if mapped else 1400),
        wire(kind.INITIALIZED, {"plan": plan}),
        events,
    )


def verify_pressure_snapshot(identity, snapshot, *, mapped, failed, byte_limit):
    """Verify actual retained bytes, pressure, fanout and prepared terminal detail."""
    from django_ray.workflow.progress.publication import (
        prepare_terminal_workflow_progress_publication,
    )

    ingress = snapshot["ingress"]
    retained = {
        "plan": snapshot["plan"],
        "nodes": [dict(node, dependencies=[]) for node in snapshot["graph"]["nodes"]],
        "edges": snapshot["graph"]["edges"],
        "recent_events": snapshot["recent_events"],
    }
    actual_bytes = len(
        json.dumps(retained, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    )
    if ingress["retained_bytes"] != actual_bytes or actual_bytes > byte_limit:
        raise ValueError("Pressure retained-byte accounting failed")
    drops = ingress["replaceable"]
    if ingress["rejected"] or not (drops["evicted_nodes"] + drops["dropped_updates"]):
        raise ValueError("Pressure fixture did not preserve lifecycle admission")
    prepared = prepare_terminal_workflow_progress_publication(
        identity,
        snapshot,
        plan_fingerprint=FINGERPRINT,
        selected_strategy="dynamic_tasks",
        reporting_policy="full",
        detail_days=7,
    )
    expected = "FAILED" if failed else "SUCCEEDED"
    if prepared.summary["state"] != expected or len(prepared.detail.records) != 1:
        raise ValueError("Pressure fixture lost terminal publication")
    detail = json.loads(prepared.detail.records[0].payload)
    if detail["state"] != expected:
        raise ValueError("Pressure fixture lost terminal detail")
    if failed and detail["error"] != "fixed pressure failure":
        raise ValueError("Pressure fixture changed failure detail")
    if mapped and detail["fanout"] != {
        "max_concurrency": 1,
        "max_items": None,
        "submitted_items": 1000,
        "completed_items": 999 if failed else 1000,
        "in_flight_items": 1 if failed else 0,
        "input_exhausted": True,
    }:
        raise ValueError("Pressure fixture changed observed map counts")
    return {"state": expected, "mapped": mapped, "byte_accounting": True, "prepared_detail": True}


def verify_deployed_pressure() -> dict:
    """Use four serial quarter-CPU actors and the source-verified mounted archive."""
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification":
        raise ValueError("Pressure fixture requires disposable qualification settings")
    import ray
    from ray.exceptions import RayActorError

    from django_ray.runtime.remote import WorkflowProgressActor

    if ray.is_initialized():
        raise ValueError("Pressure fixture requires its own Ray connection")
    deadline = time.monotonic() + 120

    def get(ref):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Pressure fixture deadline exceeded")
        return ray.get(ref, timeout=min(20, remaining))

    observations = []
    try:
        # Ray accepts a local archive at job initialization, not in actor
        # options. The resulting uploaded job environment is inherited below.
        ray.init(
            address="ray://ray-head:10001",
            runtime_env={"working_dir": "/runtime/recovery.zip"},
            logging_level="ERROR",
        )
        actor_type = ray.remote(num_cpus=0.25, max_restarts=0, max_task_retries=0)(
            WorkflowProgressActor
        )
        for mapped in (False, True):
            for failed in (False, True):
                identity = WorkflowRunIdentity(9, 1, 1, str(uuid4()))
                limits, initialization, events = pressure_case(
                    identity, mapped=mapped, failed=failed
                )
                actor = actor_type.remote(initialization, limits=limits)
                try:
                    for event in events:
                        if get(actor.ingest.remote(event)) is not True:
                            raise ValueError("Pressure fixture rejected lifecycle ingress")
                    observations.append(
                        verify_pressure_snapshot(
                            identity,
                            get(actor.snapshot.remote()),
                            mapped=mapped,
                            failed=failed,
                            byte_limit=limits.combined_max_decoded_bytes,
                        )
                    )
                finally:
                    ray.kill(actor, no_restart=True)
                    try:
                        get(actor.snapshot.remote())
                    except RayActorError:
                        pass
                    else:
                        raise ValueError("Pressure fixture actor remained available after cleanup")
        return {"scope": "collector_and_preparation", "cases": observations, "actors_removed": True}
    finally:
        ray.shutdown()
