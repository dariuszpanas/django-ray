"""Fresh, inert history observations; never a complete native upgrade receipt.

Run after the baseline history snapshot exists, with the fixture callable module
still unimported. Candidate observers use the observer-only history settings for
the real admin presentation. This module neither imports application tasks nor
starts Ray. Missing/corrupt artifact rehearsals require a separately restored
artifact identity and are deliberately not performed against this live root.
"""

from __future__ import annotations

import importlib.abc
import json
import os
import sys
from contextlib import contextmanager

from qualification.upgrade import runtime_steps as steps

_TASK_MODULE = "qualification.upgrade.runtime_tasks"


class HistoryReadError(ValueError):
    """Fixed diagnostics only, without stored payloads or provider exceptions."""


class _PoisonTaskImport(importlib.abc.MetaPathFinder):
    attempted = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname == _TASK_MODULE or fullname.startswith(_TASK_MODULE + "."):
            self.attempted = True
            raise HistoryReadError("upgrade-history-callable-imported")
        return None


@contextmanager
def _without_callable_import():
    parent = sys.modules.get("qualification.upgrade")
    if _TASK_MODULE in sys.modules or getattr(parent, "runtime_tasks", None) is not None:
        raise HistoryReadError("upgrade-history-requires-fresh-observer")
    poison = _PoisonTaskImport()
    sys.meta_path.insert(0, poison)
    try:
        yield
        if poison.attempted or _TASK_MODULE in sys.modules:
            raise HistoryReadError("upgrade-history-callable-imported")
    finally:
        sys.meta_path.remove(poison)


def _expected_result(row, case):
    marker = steps._read(
        steps._directory("runtime-effects") / f"{case}.{row.attempt_number}.committed.json"
    )
    if type(marker) is not dict or set(marker) != {
        "schema",
        "case",
        "phase",
        "observed_at",
        "identity",
    }:
        raise HistoryReadError("upgrade-history-effect-mismatch")
    identity = marker["identity"]
    if (
        marker["schema"] != 1
        or marker["case"] != case
        or marker["phase"] != "committed"
        or type(identity) is not dict
        or identity.get("task_pk") != row.pk
        or identity.get("task_id") != row.task_id
        or identity.get("attempt") != row.attempt_number
        or identity.get("generation") != row.execution_generation
        or identity.get("package_version") != "0.4.0"
        or identity.get("ray_version") != "2.56.0"
        or identity.get("context_protocol") is not None
        or not identity.get("native_job_id")
    ):
        raise HistoryReadError("upgrade-history-effect-mismatch")
    if case == "old-success":
        return {
            "identity": identity,
            "payload": steps.PRESERVED_PAYLOAD,
            "workflow_result": 42,
        }
    return identity


def _read_payloads(row, case):
    from django_ray.input_storage import load_task_input
    from django_ray.result_storage import load_result_reference
    from django_ray.runtime.runtime_env import runtime_env_for_execution
    from django_ray.runtime.runtime_env_encryption import RUNTIME_ENV_ENVELOPE_FORMAT

    expected_args = {
        "old-success": [case, steps.PRESERVED_PAYLOAD],
        "old-failure": [],
        "old-cancel": [case],
        "old-retry": [],
        "old-gated": [case],
    }[case]
    args, kwargs = load_task_input(
        args_json=row.args_json,
        kwargs_json=row.kwargs_json,
        input_reference=row.input_reference,
    )
    if args != expected_args or kwargs != {}:
        raise HistoryReadError("upgrade-history-input-mismatch")
    if case == "old-success" and (not row.input_reference or not row.result_reference):
        raise HistoryReadError("upgrade-history-external-payload-missing")
    envelope = json.loads(row.runtime_env_json)
    if type(envelope) is not dict or envelope.get("format") != RUNTIME_ENV_ENVELOPE_FORMAT:
        raise HistoryReadError("upgrade-history-encrypted-environment-missing")
    resolved = runtime_env_for_execution(row)
    variables = resolved.spec.get("env_vars", {})
    if (
        resolved.profile != "upgrade"
        or resolved.digest != row.runtime_env_hash
        or variables.get("DJANGO_RAY_UPGRADE_BUILD") != "baseline"
        or variables.get("DJANGO_RAY_UPGRADE_DATABASE") != "primary"
        or "RAY_ADDRESS" in variables
        or any(
            variables.get(name) != os.environ.get(name)
            for name in (
                "DJANGO_RAY_UPGRADE_POSTGRES_PASSWORD_FILE",
                "DJANGO_RAY_UPGRADE_ENCRYPTION_KEY_FILE",
                "DJANGO_SECRET_KEY_FILE",
            )
        )
    ):
        raise HistoryReadError("upgrade-history-environment-mismatch")
    expected = None
    if row.state == "SUCCEEDED":
        serialized = row.result_data
        if row.result_reference:
            serialized = load_result_reference(str(row.result_reference))
        if not isinstance(serialized, str) or len(serialized.encode()) > steps.MAX_BYTES:
            raise HistoryReadError("upgrade-history-result-unavailable")
        expected = _expected_result(row, case)
        if steps._json(json.loads(serialized)) != steps._json(expected):
            raise HistoryReadError("upgrade-history-result-mismatch")
    return args, kwargs, expected


def _legacy_progress(row, *, candidate):
    # The released wheel predates the workflow package split. Select its actual
    # public compatibility reader after the observer has verified the epoch;
    # do not let a fallback hide a broken current installation.
    if candidate:
        from django_ray.workflow.progress.runs import (
            WorkflowProgressReadSource,
            read_workflow_progress,
        )

        legacy_source = WorkflowProgressReadSource.LEGACY
    else:
        released = importlib.import_module("django_ray.workflow_progress")
        legacy_source = released.WorkflowProgressReadSource.LEGACY
        read_workflow_progress = released.read_workflow_progress

    progress = read_workflow_progress(row)
    payload = progress.payload
    if (
        progress.source is not legacy_source
        or progress.schema_version != 2
        or progress.diagnostic_code is not None
        or type(payload) is not dict
        or payload.get("state") != "SUCCEEDED"
        or any(
            type(payload.get(key)) is not int or payload[key] != value
            for key, value in {
                "total_nodes": 2,
                "completed_nodes": 2,
                "failed_nodes": 0,
                "running_nodes": 0,
                "pending_nodes": 0,
            }.items()
        )
        or type(payload.get("graph")) is not dict
        or type(payload["graph"].get("nodes")) is not list
        or len(payload["graph"]["nodes"]) != 2
        or type(payload["graph"].get("edges")) is not list
        or len(payload["graph"]["edges"]) != 1
    ):
        raise HistoryReadError("upgrade-history-legacy-progress-mismatch")
    observation = {"schema": 2, "nodes": 2, "edges": 1, "succeeded": 2}
    if not candidate:
        return observation | {"presentation": "not_run", "bounded_graph_available": False}

    from django.contrib.admin import AdminSite
    from django.http import HttpRequest

    from django_ray.admin import RayTaskExecutionAdmin
    from django_ray.models import RayTaskExecution
    from django_ray.workflow.progress.reads import get_workflow_progress_summary

    def authorize(execution):
        return (
            execution.pk == row.pk
            and execution.task_id == row.task_id
            and execution._state.db == row._state.db
        )

    envelope = get_workflow_progress_summary(
        row,
        authorize=authorize,
        include_legacy=True,
        attempt_number=row.attempt_number,
        infer_current_reporting_policy=False,
    )
    summary = envelope.get("summary")
    if (
        envelope.get("source_schema_version") != 2
        or envelope.get("availability") != "NOT_REPORTED"
        or envelope.get("complete") is not False
        or type(summary) is not dict
        or summary.get("node_counts")
        != {
            "declared": 2,
            "discovered": 2,
            "retained_topology": 0,
            "retained_detail": 0,
            "pending": 0,
            "running": 0,
            "succeeded": 2,
            "failed": 0,
        }
        or summary.get("edge_counts")
        != {
            "declared": 1,
            "discovered": 1,
            "retained_topology": 0,
        }
    ):
        raise HistoryReadError("upgrade-history-summary-mismatch")

    class FixtureHistoryAdmin(RayTaskExecutionAdmin):
        def has_view_permission(self, request, obj=None):
            return obj is not None and authorize(obj)

    presentation = FixtureHistoryAdmin(
        RayTaskExecution, AdminSite()
    )._lazy_workflow_progress_presentation(
        HttpRequest(),
        row,
        {},
        None,
        attempt_number=row.attempt_number,
    )
    if (
        presentation.get("state") != "LEGACY_ONLY"
        or presentation.get("complete") is not False
        or presentation.get("actions")
        != {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }
    ):
        raise HistoryReadError("upgrade-history-presentation-mismatch")
    return observation | {"presentation": "LEGACY_ONLY", "bounded_graph_available": False}


def observe_runtime_history() -> dict:
    """Read five fixed old cases without importing their application module.

    Baseline reads use the released storage APIs because its get_result eagerly
    imports the callable. Candidate reads additionally exercise public get_result,
    the inert result-task refusal and authoritative unsupported-protocol retry.
    Successful return proves these observations only, never native cleanup.
    """
    build = os.environ.get("DJANGO_RAY_UPGRADE_BUILD")
    database = os.environ.get("DJANGO_RAY_UPGRADE_DATABASE")
    if build not in {"baseline", "candidate"} or database not in {"primary", "scratch"}:
        raise HistoryReadError("upgrade-history-epoch-mismatch")
    try:
        with _without_callable_import():
            steps._epoch(build, database)
            before = steps.history(compare=True)
            all_fields_before = steps._rows_snapshot()
            rows = [steps._task(case) for case in steps.OLD_CASES]
            candidate = build == "candidate"
            for case, row in zip(steps.OLD_CASES, rows, strict=True):
                args, kwargs, expected = _read_payloads(row, case)
                if not candidate:
                    continue
                from django.tasks import TaskResultStatus, task_backends

                from django_ray.lifecycle import TaskRetryRequestStatus, request_task_retry

                result = task_backends["jobs"].get_result(row.task_id)
                expected_status = (
                    TaskResultStatus.SUCCESSFUL
                    if row.state == "SUCCEEDED"
                    else TaskResultStatus.FAILED
                )
                if (
                    result.id != row.task_id
                    or result.args != args
                    or result.kwargs != kwargs
                    or result.status is not expected_status
                    or result.task.module_path != row.callable_path
                ):
                    raise HistoryReadError("upgrade-history-public-result-mismatch")
                if row.state == "SUCCEEDED" and steps._json(result.return_value) != steps._json(
                    expected
                ):
                    raise HistoryReadError("upgrade-history-public-result-mismatch")
                if row.state == "FAILED" and not result.errors:
                    raise HistoryReadError("upgrade-history-public-error-missing")
                try:
                    result.task.enqueue()
                except TypeError as error:
                    if "reconstructed from a durable result is read-only" not in str(error):
                        raise HistoryReadError("upgrade-history-enqueue-refusal-mismatch") from None
                else:
                    raise HistoryReadError("upgrade-history-reenqueue-accepted")
                outcome = request_task_retry(
                    row.pk,
                    expected_attempt_number=row.attempt_number,
                    expected_execution_generation=row.execution_generation,
                    expected_workflow_identity=(
                        str(row.workflow_run_id),
                        str(row.workflow_plan_fingerprint)
                        if row.workflow_plan_fingerprint is not None
                        else None,
                    )
                    if row.workflow_run_id is not None
                    else None,
                )
                if outcome.status is not TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL:
                    raise HistoryReadError("upgrade-history-retry-refusal-mismatch")
            workflow = _legacy_progress(rows[0], candidate=candidate)
            after = steps.history(compare=True)
            if before != after or all_fields_before != steps._rows_snapshot():
                raise HistoryReadError("upgrade-history-read-mutated-rows")
            return {
                "schema": 1,
                "build": build,
                "database": database,
                "cases_read": len(rows),
                "encrypted_environments_read": len(rows),
                "external_input_read": True,
                "external_result_read": True,
                "public_results_read": len(rows) if candidate else 0,
                "inert_reenqueue_refusals": len(rows) if candidate else 0,
                "unsupported_retry_refusals": len(rows) if candidate else 0,
                "history_unchanged": True,
                **after,
                "workflow": workflow,
                "artifact_negative_checks": "not_run",
                "artifact_negative_reason": "independent_restored_artifact_identity_required",
                "workflow_presentation_verified": candidate,
                "rendered_workflow_verified": False,
                "workflow_html_render": "not_run",
            }
    except HistoryReadError:
        raise
    except Exception:
        raise HistoryReadError("upgrade-history-read-failed") from None
