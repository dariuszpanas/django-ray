"""Supplement the frozen 45180ea application receipt validators, without execution.

The caller still runs run_chainsaw.parse_receipts for every receipt and establishes
source, job, artifact and cleanup provenance independently. These checks cover
retained setup, node, Core and workflow facts that that parser only shape-checks;
they do not reconstruct discarded live HTTP, database or browser observations.
"""

from __future__ import annotations

import math
import re
from typing import Any, cast
from uuid import UUID

from qualification.application.lease_policy import validate_evidence as validate_lease_policy
from qualification.application.runtime_env_maintenance import (
    validate_evidence as validate_runtime_env_maintenance,
)
from qualification.application.workflow_fixtures import COMPLEX_EDGES, recovery_expectation
from scripts.test_planning_results import NormalizationError, equal, require, sha

COMMON = {"schema_version", "layer", "status", "complete_application_gate"}
OTHER_NAMES = {"reporting"} | {
    f"{generation}-{layer}"
    for generation in ("before", "after")
    for layer in ("first", "rq2", "showcase", "protocol")
}
NODE = re.compile(r"[0-9a-f]{56}")
HOST = re.compile(r"django-manager-[a-z0-9-]{1,48}")
MAX_INTEGER = 2**53 - 1
API_FACTS = {
    "api_task_status_bounded",
    "api_bulk_reset_absent",
    "api_legacy_workflow_node_absent",
    "api_execution_delete_rejected",
    "api_legacy_workflow_graph_absent",
}
CASES = {
    "full-success": ("full", ("SUCCEEDED",)),
    "full-failure": ("full", ("FAILED",)),
    "terminal_only-success": ("terminal_only", ("SUCCEEDED",)),
    "terminal_only-failure": ("terminal_only", ("FAILED",)),
    "disabled-success": ("disabled", ("SUCCEEDED",)),
    "disabled-failure": ("disabled", ("FAILED",)),
    "recovery": ("full", ("FAILED", "FAILED", "SUCCEEDED")),
    "plan-overflow": ("full", ("SUCCEEDED",)),
    "admin-display-limit": ("full", ("SUCCEEDED",)),
    "retry-success": ("full", ("SUCCEEDED",)),
    "retry-exhausted": ("full", ("FAILED", "FAILED", "FAILED")),
    "retry-unlimited": ("full", ("SUCCEEDED",)),
}


def _object(value: object, fields: set[str]) -> dict[str, Any]:
    require(type(value) is dict and set(value) == fields, "application payload fields differ")
    return cast(dict[str, Any], value)


def _integer(value: object, minimum: int = 0, maximum: int = MAX_INTEGER) -> None:
    require(
        type(value) is int and minimum <= value <= maximum,
        "application count is invalid",
    )


def _uuid(value: object, *, task: bool = False) -> None:
    require(type(value) is str and len(value) == 36, "application UUID is invalid")
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise NormalizationError("application UUID is invalid") from None
    require(str(parsed) == value and (not task or parsed.version == 4), "application UUID differs")


def _archives(value: object) -> None:
    archives = _object(value, {"source", "recovery"})
    for archive in archives.values():
        identity = _object(archive, {"bytes", "sha256"})
        _integer(identity["bytes"], 1, 32 * 1024 * 1024)
        require(sha(identity["sha256"]), "application archive digest is invalid")


def _setup(value: dict) -> None:
    _object(
        value,
        COMMON
        | {
            "postgresql_major",
            "migrations_applied",
            "static_collected",
            "administrator_created",
            "archives",
        },
    )
    equal(value["postgresql_major"], 17, "application PostgreSQL version differs")
    for key in ("migrations_applied", "static_collected", "administrator_created"):
        require(value[key] is True, "application setup is incomplete")
    _archives(value["archives"])


def _nodes(name: str, value: dict) -> None:
    _object(value, COMMON | {"cold_replacement", "previous_receipt_sha256", "observations"})
    after = name == "after-nodes"
    require(value["cold_replacement"] is after, "application cold generation differs")
    require(
        sha(value["previous_receipt_sha256"])
        if after
        else value["previous_receipt_sha256"] is None,
        "application predecessor digest is invalid",
    )
    observations = value["observations"]
    require(
        type(observations) is list and len(observations) == 2, "application nodes are incomplete"
    )
    ids = set()
    facts = None
    for observation in observations:
        node = _object(
            observation,
            {
                "node_id",
                "python_minor",
                "ray_version",
                "django_ray_preinstalled",
                "remote_sha256",
                "archives",
            },
        )
        require(
            type(node["node_id"]) is str and NODE.fullmatch(node["node_id"]) is not None,
            "application node identity is invalid",
        )
        ids.add(node["node_id"])
        equal(node["python_minor"], [3, 12], "application node Python differs")
        equal(node["ray_version"], "2.58.0", "application node Ray differs")
        require(node["django_ray_preinstalled"] is False, "application generic node has package")
        require(sha(node["remote_sha256"]), "application remote digest is invalid")
        _archives(node["archives"])
        current = {key: item for key, item in node.items() if key != "node_id"}
        if facts is not None:
            equal(current, facts, "application node runtime facts differ")
        facts = current
    require(len(ids) == 2, "application node identities repeat")


def _core(value: dict) -> None:
    _object(
        value,
        COMMON
        | {
            "failed_stage",
            "api",
            "executions",
            "dashboard",
            "runtime_env_refusals",
            "lease_policy",
            "runtime_env_maintenance",
            "protected_runtime",
            "ordinary_results",
        },
    )
    require(value["failed_stage"] is None, "application Core contains a failed stage")
    for field, validator in (
        ("lease_policy", validate_lease_policy),
        ("runtime_env_maintenance", validate_runtime_env_maintenance),
    ):
        try:
            validator(value[field])
        except ValueError:
            raise NormalizationError(f"application {field} evidence is incomplete") from None
    api = _object(value["api"], API_FACTS | {"task_id", "task_state", "task_result"})
    _uuid(api["task_id"], task=True)
    equal(api["task_state"], "SUCCEEDED", "application API task failed")
    equal(api["task_result"], 5, "application API result differs")
    require(all(api[key] is True for key in API_FACTS), "application API facts are incomplete")
    equal(
        value["dashboard"],
        {
            "status": "passed",
            "anonymous_admin_denied": True,
            "admin_task_link_verified": True,
            "proxy_base_path_verified": True,
            "task_api_identity_verified": True,
            "task_detail_rendered": True,
            "javascript_errors": 0,
            "session_and_proxy_removed": True,
        },
        "application dashboard evidence differs",
    )
    executions = value["executions"]
    require(
        type(executions) is list and len(executions) == 2, "application executions are incomplete"
    )
    for entry in executions:
        execution = _object(
            entry,
            {
                "task_id",
                "execution_id",
                "attempt_number",
                "execution_generation",
                "worker_id",
                "worker_hostname",
                "django_ray_version",
                "encrypted_snapshot_authenticated",
                "elapsed_seconds",
            },
        )
        _uuid(execution["task_id"], task=True)
        _uuid(execution["worker_id"])
        _integer(execution["execution_id"], 1)
        _integer(execution["execution_generation"], 1)
        equal(execution["attempt_number"], 1, "application execution retried")
        equal(execution["django_ray_version"], "0.7.0", "application installed version differs")
        require(
            execution["encrypted_snapshot_authenticated"] is True, "application snapshot unverified"
        )
        require(
            type(execution["worker_hostname"]) is str
            and HOST.fullmatch(execution["worker_hostname"]) is not None,
            "application manager identity differs",
        )
        elapsed = execution["elapsed_seconds"]
        require(
            type(elapsed) in (int, float)
            and 0 <= elapsed <= MAX_INTEGER
            and math.isfinite(elapsed),
            "application execution time is invalid",
        )
    require(executions[0]["task_id"] == api["task_id"], "application API execution differs")
    for key in ("task_id", "execution_id"):
        require(executions[0][key] != executions[1][key], "application probe execution repeats")
    protected = value["protected_runtime"]
    require(
        type(protected) is dict
        and type(protected.get("row_ids")) is list
        and len(protected["row_ids"]) == 3,
        "application protected rows are absent",
    )
    equal(
        protected["row_ids"][0],
        executions[1]["execution_id"],
        "application protected probe differs",
    )


def _identity(value: object, attempt: int) -> None:
    identity = _object(
        value, {"schema_version", "run_id", "attempt_number", "execution_generation"}
    )
    equal(identity["schema_version"], 1, "workflow identity version differs")
    equal(identity["attempt_number"], attempt, "workflow attempt sequence differs")
    _integer(identity["execution_generation"])
    _uuid(identity["run_id"])


def _browser(value: object, policy: str, attempts: int) -> None:
    equal(
        value,
        {
            "status": "passed",
            "attempts_rendered": attempts,
            "policy": policy,
            "javascript_errors": 0,
            "diagnostics_preserved": True,
        },
        "workflow browser evidence differs",
    )


def _failure_ancestry(case: str, attempt: int) -> tuple[int, int]:
    """Count the fixed failed origin's ancestors from the source-owned topology."""
    if case == "recovery":
        states, edges = recovery_expectation(attempt)
        origins = {node for node, state in states.items() if state == "FAILED"}
    elif case == "full-failure":
        # The fast branch may vary; the failed slow branch and its ancestors cannot.
        origins, edges = {"0.1.g1.1"}, COMPLEX_EDGES
    elif case == "retry-exhausted":
        origins, edges = {"0"}, frozenset()
    else:
        origins, edges = set(), frozenset()
    ancestors = set(origins)
    pending = list(origins)
    while pending:
        target = pending.pop()
        for source, destination in edges:
            if destination == target and source not in ancestors:
                ancestors.add(source)
                pending.append(source)
    return len(ancestors), sum(target in origins for _source, target in edges)


def _admin(observation: dict, case: str, attempt: int) -> None:
    admin = observation["admin_contract"]
    base = {
        "task_id": observation["task_id"],
        "task_state": observation["state"],
        "attempt_number": attempt,
        "diagnostics_preserved": True,
    }
    if observation["reporting_policy"] == "terminal_only":
        equal(
            admin,
            {
                **base,
                "admin_actions": 0,
                "admin_workflow": "terminal-summary-verified",
                "attempt_summary_matches": True,
                "declared_edges": 13,
                "declared_nodes": 13,
                "detail_availability": "OMITTED_BY_POLICY",
                "graph_advertised": False,
                "graph_status": "UNAVAILABLE",
                "legacy_progress_null": True,
                "manifest_links": 0,
                "node_details": 0,
                "reporting_policy": "terminal_only",
                "storage_rows": 0,
                "summary_revision": 1,
                "topology_manifests": 0,
                "topology_pages": 0,
            },
            "workflow terminal-only Admin evidence differs",
        )
        return
    counts = observation["counts"]
    fixed = {
        **base,
        "admin_workflow": "verified",
        "admin_routes": 6,
        "admin_actions": 2 if case == "retry-exhausted" else 3,
        "topology_nodes": counts["nodes"],
        "topology_edges": counts["edges"],
        "node_details": counts["nodes"],
        "graph_status": "AVAILABLE",
        "graph_nodes": counts["nodes"],
        "graph_edges": counts["edges"],
        "current_manifests": 1,
        "pending_manifests": 0,
        "unlinked_pages": 0,
        "graph_preview_contract": "not-applicable",
    }
    numeric = {
        f"graph_{name}"
        for name in (
            "pending_nodes",
            "running_nodes",
            "succeeded_nodes",
            "failed_nodes",
            "failure_path_nodes",
            "failure_origins",
            "incoming_failure_edges",
            "available_previews",
            "failed_previews",
            "unavailable_previews",
        )
    }
    admin = _object(admin, set(fixed) | numeric)
    equal({key: admin[key] for key in fixed}, fixed, "workflow Admin facts differ")
    for key in numeric:
        _integer(admin[key], 0, 256)
    nodes = counts["nodes"]
    require(
        sum(
            admin[f"graph_{state}_nodes"] for state in ("pending", "running", "succeeded", "failed")
        )
        == nodes,
        "workflow Admin node states differ",
    )
    require(
        sum(admin[f"graph_{kind}_previews"] for kind in ("available", "failed", "unavailable"))
        <= nodes,
        "workflow Admin previews exceed nodes",
    )
    require(
        admin["graph_failure_path_nodes"] <= nodes
        and admin["graph_incoming_failure_edges"] <= counts["edges"],
        "workflow failure graph counts differ",
    )
    if case == "recovery":
        states, _ = recovery_expectation(attempt)
        expected = {
            state: list(states.values()).count(state.upper())
            for state in ("pending", "running", "succeeded", "failed")
        }
    elif observation["state"] == "SUCCEEDED":
        expected = {"pending": 0, "running": 0, "succeeded": nodes, "failed": 0}
    elif case == "retry-exhausted":
        expected = {"pending": 0, "running": 0, "succeeded": 0, "failed": 1}
    else:
        # verify_complex_graph deliberately allows scheduling variation on failure.
        expected = {"failed": 1}
        require(admin["graph_succeeded_nodes"] >= 2, "workflow failure lost completed ancestors")
    for state, count in expected.items():
        equal(admin[f"graph_{state}_nodes"], count, "workflow Admin state counts differ")
    failed = observation["state"] == "FAILED"
    equal(admin["graph_failure_origins"], int(failed), "workflow failure origin differs")
    ancestry, incoming = _failure_ancestry(case, attempt)
    equal(admin["graph_failure_path_nodes"], ancestry, "workflow failure ancestry differs")
    equal(admin["graph_incoming_failure_edges"], incoming, "workflow incoming failure edges differ")


def _workflow_observation(observation: object, case: str, attempt: int) -> dict:
    policy, states = CASES[case]
    fields = {
        "task_id",
        "run_identity",
        "publication",
        "state",
        "reporting_policy",
        "complete_workflow_gate",
    }
    if case == "admin-display-limit":
        fields |= {"api_counts", "admin_status", "fixture_graph_verified", "diagnostics_preserved"}
    else:
        fields |= {"counts", "api_admin_graph_match"}
        if policy != "disabled":
            fields |= {"fixture_graph_verified", "admin_contract"}
    if attempt == len(states):
        fields.add("browser_contract")
    value = _object(observation, fields)
    _uuid(value["task_id"], task=True)
    equal(value["state"], states[attempt - 1], "workflow outcome differs")
    equal(value["reporting_policy"], policy, "workflow reporting policy differs")
    require(value["complete_workflow_gate"] is False, "workflow scope differs")
    if attempt == len(states):
        _browser(value["browser_contract"], policy, len(states))
    if policy == "disabled":
        require(
            value["run_identity"] is None and value["publication"] is None,
            "disabled workflow published",
        )
    else:
        _identity(value["run_identity"], attempt)
        publication = _object(
            value["publication"], {"summary_revision", "topology_version", "detail_revision"}
        )
        if policy == "terminal_only":
            equal(
                publication,
                {"summary_revision": 1, "topology_version": None, "detail_revision": None},
                "terminal workflow publication differs",
            )
        else:
            for revision in publication.values():
                _integer(revision, 1)
        require(value["fixture_graph_verified"] is True, "workflow fixture is unverified")
    if case == "admin-display-limit":
        equal(
            value["api_counts"],
            {"nodes": 101, "edges": 100, "details": 101},
            "workflow display-limit API differs",
        )
        equal(value["admin_status"], "LIMIT_EXCEEDED", "workflow display limit differs")
        require(value["diagnostics_preserved"] is True, "workflow diagnostics changed")
        return value
    require(value["api_admin_graph_match"] is True, "workflow API and Admin differ")
    counts = _object(value["counts"], {"nodes", "edges"})
    for count in counts.values():
        _integer(count, 0, 256)
    if policy != "full":
        expected_counts = {"nodes": 0, "edges": 0}
    elif case == "recovery":
        nodes, edges = recovery_expectation(attempt)
        expected_counts = {"nodes": len(nodes), "edges": len(edges)}
    elif case == "full-failure":
        require(
            3 <= counts["nodes"] <= 6 and counts["edges"] == counts["nodes"] - 1,
            "workflow failure topology differs",
        )
        expected_counts = counts
    else:
        nodes, edges = {
            "full-success": (8, 8),
            "plan-overflow": (65, 64),
            "retry-success": (2, 1),
            "retry-unlimited": (2, 1),
            "retry-exhausted": (1, 0),
        }[case]
        expected_counts = {"nodes": nodes, "edges": edges}
    equal(counts, expected_counts, "workflow topology counts differ")
    if policy != "disabled":
        _admin(value, case, attempt)
    return value


def _workflows(value: dict) -> None:
    _object(
        value,
        COMMON
        | {
            "complete_workflow_gate",
            "failed_stage",
            "http_transport_recoveries",
            "publisher",
            "observations",
            "collector_pressure",
        },
    )
    require(
        value["complete_workflow_gate"] is False and value["failed_stage"] is None,
        "workflow scope or failed stage differs",
    )
    equal(value["publisher"], "default_terminal", "workflow publisher differs")
    recoveries = _object(
        value["http_transport_recoveries"], {"connection_reset", "remote_disconnected"}
    )
    for count in recoveries.values():
        _integer(count)
    equal(
        value["collector_pressure"],
        {
            "scope": "collector_and_preparation",
            "actors_removed": True,
            "cases": [
                {"state": state, "mapped": mapped, "byte_accounting": True, "prepared_detail": True}
                for mapped in (False, True)
                for state in ("SUCCEEDED", "FAILED")
            ],
        },
        "workflow collector pressure is incomplete",
    )
    observations = _object(value["observations"], set(CASES))
    task_ids = set()
    run_ids = set()
    for case, (_, states) in CASES.items():
        attempts = observations[case]
        require(
            type(attempts) is list and len(attempts) == len(states),
            "workflow attempts are incomplete",
        )
        task_id = None
        for number, observation in enumerate(attempts, 1):
            checked = _workflow_observation(observation, case, number)
            if task_id is not None:
                equal(checked["task_id"], task_id, "workflow attempts changed task")
            task_id = checked["task_id"]
            if checked["run_identity"] is not None:
                run_id = checked["run_identity"]["run_id"]
                require(run_id not in run_ids, "workflow run identity repeats")
                run_ids.add(run_id)
        require(task_id not in task_ids, "workflow fixture task repeats")
        task_ids.add(task_id)


def validate_application_payload(name: str, value: dict) -> None:
    """Validate supplemental retained facts; the source receipt parser is also required."""
    require(type(name) is str, "application receipt name is invalid")
    if name in OTHER_NAMES:
        return
    layer = {
        "setup": "application_setup",
        "nodes": "generic_ray_nodes",
        "core": "application_core",
        "workflows": "workflow_api_admin",
    }.get(name.rsplit("-", 1)[-1])
    require(
        name == "setup"
        or name
        in {
            f"{generation}-{kind}"
            for generation in ("before", "after")
            for kind in ("nodes", "core", "workflows")
        },
        "application receipt name is unsupported",
    )
    require(type(value) is dict and COMMON <= set(value), "application receipt envelope is absent")
    equal(
        {key: value[key] for key in COMMON},
        {
            "schema_version": 1,
            "layer": layer,
            "status": "passed",
            "complete_application_gate": False,
        },
        "application receipt envelope differs",
    )
    if name == "setup":
        _setup(value)
    elif name.endswith("-nodes"):
        _nodes(name, value)
    elif name.endswith("-core"):
        _core(value)
    else:
        _workflows(value)


def validate_application_generations(
    before: dict,
    after: dict,
    setup: dict,
    *,
    before_core: dict,
    after_core: dict,
    before_workflows: dict,
    after_workflows: dict,
) -> None:
    """Require new cold-generation executions; the caller also checks predecessor bytes."""
    validate_application_payload("setup", setup)
    validate_application_payload("before-nodes", before)
    validate_application_payload("after-nodes", after)
    first, second = before["observations"], after["observations"]
    require(
        {node["node_id"] for node in first}.isdisjoint(node["node_id"] for node in second),
        "application cold replacement reused a node",
    )
    facts = {key: item for key, item in first[0].items() if key != "node_id"}
    for node in (*first, *second):
        equal(
            {key: item for key, item in node.items() if key != "node_id"},
            facts,
            "application cold replacement changed runtime facts",
        )
        equal(node["archives"], setup["archives"], "application node archives differ from setup")
    identities = []
    for generation, core, workflows in (
        ("before", before_core, before_workflows),
        ("after", after_core, after_workflows),
    ):
        validate_application_payload(f"{generation}-core", core)
        validate_application_payload(f"{generation}-workflows", workflows)
        task_ids = {execution["task_id"] for execution in core["executions"]}
        execution_ids = {execution["execution_id"] for execution in core["executions"]}
        for row_id in core["protected_runtime"]["row_ids"]:
            _integer(row_id, 1)
            execution_ids.add(row_id)
        run_ids = set()
        for observations in workflows["observations"].values():
            for observation in observations:
                task_ids.add(observation["task_id"])
                if observation["run_identity"] is not None:
                    run_ids.add(observation["run_identity"]["run_id"])
        identities.append((task_ids, execution_ids, run_ids))
    for previous, current, kind in zip(
        identities[0], identities[1], ("task", "execution", "run"), strict=True
    ):
        require(previous.isdisjoint(current), f"application cold generation reused {kind} identity")
