"""Offline retained-payload regressions; fixtures do not establish live CI proof."""

import json
from pathlib import Path

import pytest

from qualification.application.lease_policy import FACTS as LEASE_FACTS
from qualification.application.runtime_env_maintenance import FACTS as MAINTENANCE_FACTS
from scripts.test_planning_application_receipts import (
    validate_application_generations,
    validate_application_payload,
)
from scripts.test_planning_receipts import _native_observations
from scripts.test_planning_results import NormalizationError

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/test_planning_receipts/files.json"
OWNED = (
    "setup",
    "before-nodes",
    "after-nodes",
    "before-core",
    "after-core",
    "before-workflows",
    "after-workflows",
)
COMMON = {"schema_version", "layer", "status", "complete_application_gate"}


def receipt(name):
    context = json.loads(FIXTURE.with_name("context.json").read_bytes())
    prefix = f"application-core-{context['run_id']}/application-evidence/"
    return json.loads(json.loads(FIXTURE.read_bytes())[prefix + name + ".json"])


def generation_payloads():
    return {
        f"{generation}_{layer}": receipt(f"{generation}-{layer}")
        for generation in ("before", "after")
        for layer in ("core", "workflows")
    }


def change(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


@pytest.mark.parametrize("name", OWNED)
def test_retained_payloads_are_accepted(name):
    validate_application_payload(name, receipt(name))


def test_retained_generations_are_linked():
    validate_application_generations(
        receipt("before-nodes"), receipt("after-nodes"), receipt("setup"), **generation_payloads()
    )


@pytest.mark.parametrize("name", OWNED)
def test_passed_envelope_alone_is_not_payload_evidence(name):
    value = {key: item for key, item in receipt(name).items() if key in COMMON}
    # This formerly passed parse_receipts, including the cold/hash fields.
    if name == "after-nodes":
        value.update(cold_replacement=True, previous_receipt_sha256="a" * 64)
    with pytest.raises(NormalizationError):
        validate_application_payload(name, value)


@pytest.mark.parametrize("name", OWNED)
def test_each_required_top_level_field_is_required(name):
    for key in receipt(name):
        value = receipt(name)
        del value[key]
        with pytest.raises(NormalizationError):
            validate_application_payload(name, value)


@pytest.mark.parametrize("name", OWNED)
@pytest.mark.parametrize(
    "key,bad",
    [
        ("schema_version", True),
        ("status", "failed"),
        ("complete_application_gate", 0),
        ("layer", "application_setup_future"),
    ],
)
def test_strict_common_envelope(name, key, bad):
    value = receipt(name)
    value[key] = bad
    with pytest.raises(NormalizationError):
        validate_application_payload(name, value)


@pytest.mark.parametrize(
    "name", ["before-new", "nodes", "other-core", "setup-core", "", None, 1, []]
)
def test_unknown_names_never_gain_acceptance(name):
    with pytest.raises(NormalizationError):
        validate_application_payload(name, receipt("setup"))


@pytest.mark.parametrize(
    "name",
    [
        "reporting",
        "before-first",
        "after-first",
        "before-rq2",
        "after-rq2",
        "before-showcase",
        "after-showcase",
        "before-protocol",
        "after-protocol",
    ],
)
def test_existing_specialized_source_parser_remains_authoritative(name):
    # The caller must run parse_receipts; supplemental validation does not
    # substitute a second implementation of those specialized contracts.
    validate_application_payload(name, receipt(name))


@pytest.mark.parametrize(
    "path,bad",
    [
        (("migrations_applied",), False),
        (("static_collected",), 1),
        (("administrator_created",), False),
        (("postgresql_major",), 18),
        (("archives", "source", "bytes"), True),
        (("archives", "recovery", "bytes"), 0),
        (("archives", "source", "bytes"), 32 * 1024 * 1024 + 1),
        (("archives", "source", "sha256"), "A" * 64),
    ],
)
def test_setup_requires_typed_facts_and_bounded_archive_identities(path, bad):
    value = receipt("setup")
    change(value, path, bad)
    with pytest.raises(NormalizationError):
        validate_application_payload("setup", value)


@pytest.mark.parametrize(
    "path,bad",
    [
        (("failed_stage",), "dashboard_proxy"),
        (("api",), {"task_state": "FAILED"}),
        (("api", "task_state"), "FAILED"),
        (("api", "task_result"), 6),
        (("api", "api_task_status_bounded"), 1),
        (("dashboard",), {"status": "failed", "session_and_proxy_removed": False}),
        (("dashboard", "javascript_errors"), False),
        (("dashboard", "session_and_proxy_removed"), False),
        (("executions",), []),
        (("executions", 0, "execution_id"), True),
        (("executions", 0, "execution_generation"), 0),
        (("executions", 0, "attempt_number"), True),
        (("executions", 0, "encrypted_snapshot_authenticated"), 1),
        (("executions", 0, "elapsed_seconds"), float("nan")),
        (("executions", 0, "elapsed_seconds"), float("inf")),
        (("executions", 0, "elapsed_seconds"), True),
        (("executions", 0, "django_ray_version"), "0.7.1"),
        (("executions", 0, "worker_hostname"), "unowned-manager"),
        (("executions", 0, "task_id"), "00000000-0000-4000-8000-000000000001"),
        (("protected_runtime", "row_ids", 0), 99999),
    ],
)
def test_core_rejects_contradictory_or_incomplete_retained_proof(path, bad):
    value = receipt("after-core")
    change(value, path, bad)
    with pytest.raises(NormalizationError):
        validate_application_payload("after-core", value)


def test_core_probe_must_be_a_distinct_execution():
    value = receipt("after-core")
    value["executions"][1] = value["executions"][0].copy()
    with pytest.raises(NormalizationError):
        validate_application_payload("after-core", value)


@pytest.mark.parametrize(
    "path,bad",
    [
        (("cold_replacement",), False),
        (("previous_receipt_sha256",), None),
        (("observations",), []),
        (("observations", 0, "django_ray_preinstalled"), 0),
        (("observations", 0, "python_minor"), [3, 13]),
        (("observations", 0, "ray_version"), "2.59.0"),
        (("observations", 0, "remote_sha256"), "bad"),
        (("observations", 0, "node_id"), "A" * 56),
        (("observations", 0, "archives", "source", "bytes"), True),
    ],
)
def test_nodes_require_complete_compatible_runtime_observations(path, bad):
    value = receipt("after-nodes")
    change(value, path, bad)
    with pytest.raises(NormalizationError):
        validate_application_payload("after-nodes", value)


def test_nodes_require_unique_membership():
    value = receipt("before-nodes")
    value["observations"][1] = value["observations"][0].copy()
    with pytest.raises(NormalizationError):
        validate_application_payload("before-nodes", value)


@pytest.mark.parametrize("mutation", ["reused", "remote", "archive", "setup"])
def test_cold_generation_crosslinks_fail_closed(mutation):
    before, after, setup = receipt("before-nodes"), receipt("after-nodes"), receipt("setup")
    if mutation == "reused":
        after["observations"][0]["node_id"] = before["observations"][0]["node_id"]
    elif mutation == "remote":
        for node in after["observations"]:
            node["remote_sha256"] = "b" * 64
    elif mutation == "archive":
        for node in after["observations"]:
            node["archives"]["source"]["sha256"] = "b" * 64
    else:
        setup["archives"]["source"]["sha256"] = "b" * 64
    with pytest.raises(NormalizationError):
        validate_application_generations(before, after, setup, **generation_payloads())


@pytest.mark.parametrize("layer", ["core", "workflows"])
def test_cold_generation_rejects_copied_predecessor_payload(layer):
    payloads = generation_payloads()
    payloads[f"after_{layer}"] = payloads[f"before_{layer}"]
    # Each receipt alone is valid: generation belongs to the enclosing sequence.
    validate_application_payload(f"after-{layer}", payloads[f"after_{layer}"])
    with pytest.raises(NormalizationError, match="cold generation reused"):
        validate_application_generations(
            receipt("before-nodes"), receipt("after-nodes"), receipt("setup"), **payloads
        )


@pytest.mark.parametrize(
    "kind", ["core-task", "core-row", "refusal-row", "workflow-task", "workflow-run"]
)
def test_cold_generation_rejects_one_reused_execution_identity(kind):
    payloads = generation_payloads()
    before, after = payloads["before_core"], payloads["after_core"]
    if kind == "core-task":
        after["api"]["task_id"] = before["api"]["task_id"]
        after["executions"][0]["task_id"] = before["api"]["task_id"]
    elif kind == "core-row":
        after["executions"][0]["execution_id"] = before["executions"][0]["execution_id"]
    elif kind == "refusal-row":
        after["protected_runtime"]["row_ids"][1] = before["protected_runtime"]["row_ids"][1]
    else:
        old = payloads["before_workflows"]["observations"]["full-success"][0]
        new = payloads["after_workflows"]["observations"]["full-success"][0]
        if kind == "workflow-task":
            new["task_id"] = new["admin_contract"]["task_id"] = old["task_id"]
        else:
            new["run_identity"]["run_id"] = old["run_identity"]["run_id"]
    validate_application_payload("after-core", payloads["after_core"])
    validate_application_payload("after-workflows", payloads["after_workflows"])
    with pytest.raises(NormalizationError, match="cold generation reused"):
        validate_application_generations(
            receipt("before-nodes"), receipt("after-nodes"), receipt("setup"), **payloads
        )


@pytest.mark.parametrize(
    "path,bad",
    [
        (("failed_stage",), "collector-pressure"),
        (("complete_workflow_gate",), True),
        (("publisher",), "future"),
        (("observations",), {}),
        (("collector_pressure", "actors_removed"), False),
        (("collector_pressure", "cases", 0, "mapped"), 0),
        (("http_transport_recoveries", "connection_reset"), True),
        (("observations", "recovery"), []),
        (("observations", "full-success", 0, "state"), "FAILED"),
        (("observations", "full-success", 0, "run_identity", "attempt_number"), True),
        (("observations", "full-success", 0, "publication", "summary_revision"), True),
        (("observations", "full-success", 0, "counts", "nodes"), 7),
        (("observations", "full-success", 0, "browser_contract", "javascript_errors"), False),
        (("observations", "full-success", 0, "browser_contract", "diagnostics_preserved"), False),
        (("observations", "full-success", 0, "admin_contract", "graph_succeeded_nodes"), True),
        (("observations", "full-success", 0, "admin_contract", "pending_manifests"), 1),
        (("observations", "full-success", 0, "admin_contract", "graph_failure_origins"), 1),
        (("observations", "full-success", 0, "admin_contract", "task_id"), "wrong"),
        (("observations", "full-success", 0, "admin_contract", "graph_available_previews"), 100),
        (("observations", "full-failure", 0, "admin_contract", "graph_failed_nodes"), 0),
        (("observations", "recovery", 1, "run_identity", "attempt_number"), 1),
        (("observations", "recovery", 2, "browser_contract", "attempts_rendered"), 1),
        (("observations", "disabled-success", 0, "publication"), {}),
        (("observations", "terminal_only-success", 0, "admin_contract", "storage_rows"), 1),
        (("observations", "terminal_only-failure", 0, "publication", "detail_revision"), 1),
        (("observations", "admin-display-limit", 0, "api_counts", "details"), 100),
        (("observations", "admin-display-limit", 0, "admin_status"), "AVAILABLE"),
    ],
)
def test_workflow_payload_requires_all_fixed_assertion_facts(path, bad):
    value = receipt("after-workflows")
    change(value, path, bad)
    with pytest.raises(NormalizationError):
        validate_application_payload("after-workflows", value)


def test_workflow_receipts_reject_duplicate_task_or_attempt_run():
    for field in ("task_id", "run_identity"):
        value = receipt("before-workflows")
        value["observations"]["full-failure"][0][field] = value["observations"]["full-success"][0][
            field
        ]
        with pytest.raises(NormalizationError):
            validate_application_payload("before-workflows", value)


@pytest.mark.parametrize(
    "case,index,path_nodes,incoming",
    [
        ("full-failure", 0, 1, 0),
        ("recovery", 0, 0, 0),
        ("recovery", 1, 7, 2),
        ("recovery", 2, 1, 1),
        ("retry-exhausted", 0, 0, 0),
        ("retry-exhausted", 1, 0, 0),
        ("retry-exhausted", 2, 0, 0),
    ],
)
@pytest.mark.parametrize("field", ["graph_failure_path_nodes", "graph_incoming_failure_edges"])
def test_workflow_failure_ancestry_matches_source_topology(
    case, index, path_nodes, incoming, field
):
    value = receipt("before-workflows")
    admin = value["observations"][case][index]["admin_contract"]
    bad = path_nodes if field == "graph_failure_path_nodes" else incoming
    # Some incoming counts are legitimately zero; choose another integer to
    # prove strict identity instead of only the presence of failure evidence.
    admin[field] = bad if bad != admin[field] else bad + 1
    with pytest.raises(NormalizationError, match="failure (ancestry|edges|graph counts)"):
        validate_application_payload("before-workflows", value)


def test_complex_failure_allows_source_owned_scheduling_variation():
    value = receipt("before-workflows")
    observation = value["observations"]["full-failure"][0]
    observation["counts"] = {"nodes": 3, "edges": 2}
    admin = observation["admin_contract"]
    admin.update(
        topology_nodes=3,
        topology_edges=2,
        node_details=3,
        graph_nodes=3,
        graph_edges=2,
        graph_succeeded_nodes=2,
    )
    validate_application_payload("before-workflows", value)


def test_nested_unknown_fields_fail_closed():
    value = receipt("before-core")
    value["api"]["future_api_success"] = True
    with pytest.raises(NormalizationError):
        validate_application_payload("before-core", value)


def test_current_source_contract_has_all_release_maintenance_facts():
    assert len(LEASE_FACTS) == 11
    assert sum(len(facts) for facts in MAINTENANCE_FACTS.values()) == 20


@pytest.mark.parametrize("generation", ["before", "after"])
@pytest.mark.parametrize("fact", LEASE_FACTS)
@pytest.mark.parametrize("bad", [False, 1, None])
def test_both_core_generations_require_every_strict_lease_policy_fact(generation, fact, bad):
    name = f"{generation}-core"
    value = receipt(name)
    value["lease_policy"][fact] = bad
    with pytest.raises(NormalizationError, match="lease_policy evidence is incomplete"):
        validate_application_payload(name, value)


@pytest.mark.parametrize("generation", ["before", "after"])
@pytest.mark.parametrize(
    "case,fact", [(case, fact) for case, facts in MAINTENANCE_FACTS.items() for fact in facts]
)
@pytest.mark.parametrize("bad", [False, 1, None])
def test_both_core_generations_require_every_strict_maintenance_fact(generation, case, fact, bad):
    name = f"{generation}-core"
    value = receipt(name)
    value["runtime_env_maintenance"][case][fact] = bad
    with pytest.raises(NormalizationError, match="runtime_env_maintenance evidence is incomplete"):
        validate_application_payload(name, value)


@pytest.mark.parametrize("field", ["lease_policy", "runtime_env_maintenance"])
@pytest.mark.parametrize("generation", ["before", "after"])
def test_old_or_partial_application_payloads_do_not_pass_current_contract(field, generation):
    name = f"{generation}-core"
    value = receipt(name)
    del value[field]
    with pytest.raises(NormalizationError):
        validate_application_payload(name, value)


@pytest.mark.parametrize(
    "path",
    [
        ("lease_policy",),
        ("runtime_env_maintenance",),
        *(("runtime_env_maintenance", case) for case in MAINTENANCE_FACTS),
    ],
)
def test_release_maintenance_payloads_reject_future_fields(path):
    value = receipt("before-core")
    target = value
    for key in path:
        target = target[key]
    target["future_proof"] = True
    with pytest.raises(NormalizationError):
        validate_application_payload("before-core", value)


def native_observations(variant, backend, phase="candidate-run"):
    files = json.loads(FIXTURE.read_bytes())
    manifest = json.loads(
        files[f"upgrade-native-qualification-{variant}/upgrade-receipts/execution-manifest.json"]
    )
    receipt = next(item for item in manifest["backends"] if item["backend"] == backend)
    return next(item["observations"] for item in receipt["phases"] if item["phase"] == phase)


@pytest.mark.parametrize("variant", ["native", "jobs", "jobs-crash", "core-loss"])
@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_current_native_candidate_contracts_accept_actual_receipts(variant, backend):
    _native_observations(
        "candidate-run",
        backend,
        "ray_job" if variant.startswith("jobs") else "ray_core",
        variant in {"jobs-crash", "core-loss"},
        native_observations(variant, backend),
    )


@pytest.mark.parametrize("variant", ["jobs-crash", "core-loss"])
@pytest.mark.parametrize("transition", ["before_crash", "after_drain"])
@pytest.mark.parametrize(
    "field,bad",
    [
        ("expected_seconds", 59),
        ("expected_revision", 0),
        ("lease_seconds", 5),
        ("revision", 4),
        ("history_preserved", False),
        ("leases_preserved", 1),
        ("active_leases", True),
        ("nonterminal", 1),
    ],
)
def test_native_public_lease_cas_requires_exact_stopped_cohort_proof(
    variant, transition, field, bad
):
    value = native_observations(variant, "sqlite")
    value["lease_policy"][transition][field] = bad
    with pytest.raises(NormalizationError, match="invalid-native-phase-observations"):
        _native_observations(
            "candidate-run",
            "sqlite",
            "ray_job" if variant == "jobs-crash" else "ray_core",
            True,
            value,
        )


@pytest.mark.parametrize("variant", ["jobs-crash", "core-loss"])
@pytest.mark.parametrize("missing", ["lease_policy", "before_crash", "after_drain"])
def test_native_crash_receipts_require_both_public_cas_transitions(variant, missing):
    value = native_observations(variant, "postgresql")
    if missing == "lease_policy":
        del value[missing]
    else:
        del value["lease_policy"][missing]
    with pytest.raises(NormalizationError, match="invalid-native-phase-observations"):
        _native_observations(
            "candidate-run",
            "postgresql",
            "ray_job" if variant == "jobs-crash" else "ray_core",
            True,
            value,
        )


@pytest.mark.parametrize("variant", ["native", "jobs"])
def test_native_noncrash_cohorts_reject_unrequested_lease_transition(variant):
    value = native_observations(variant, "sqlite")
    value["lease_policy"] = native_observations("jobs-crash", "sqlite")["lease_policy"]
    with pytest.raises(NormalizationError, match="invalid-native-phase-observations"):
        _native_observations(
            "candidate-run", "sqlite", "ray_job" if variant == "jobs" else "ray_core", False, value
        )
