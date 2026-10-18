"""Offline parser tests; synthetic provenance never establishes live CI success."""

import hashlib
import json
import os
import subprocess
import traceback
import urllib.request
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scripts import test_planning_results as results

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/test_planning_receipts"
APP = "application-core-39101458923/"
MONITOR = "monitoring-qualification-39101458923/"
NATIVE = "upgrade-native-qualification-native/upgrade-receipts/execution-manifest.json"
LATENCY = "job-latency-qualification/latency-receipts/execution-manifest.json"
CANCEL = "cancellation-qualification/cancellation-receipts/execution-manifest.json"


def fixture_context():
    return json.loads((FIXTURES / "context.json").read_bytes())


def context(attempt=1):
    fixture = fixture_context()
    observation = next(row for row in fixture["observations"] if row["attempt"] == attempt)
    return results.ExpectedRun(
        **{
            key: fixture[key]
            for key in (
                "policy_sha256",
                "bindings_sha256",
                "execution_commit",
                "execution_tree",
                "run_id",
            )
        },
        plan_digest=observation["plan_digest"],
        attempt=attempt,
        selected=tuple(results.Cell(**cell) for cell in observation["selected"]),
    )


def inventory(files):
    return tuple(
        results.VerifiedFile(path, len(raw), hashlib.sha256(raw).hexdigest())
        for path, raw in sorted(files.items())
    )


def acquisition(expected, files):
    producers = {
        results.Cell(**producer["cell"]): producer
        for observation in fixture_context()["observations"]
        for producer in observation["producers"]
    }
    return results.VerifiedRun(
        expected.execution_commit,
        expected.execution_tree,
        expected.run_id,
        expected.attempt,
        results.CONTRACT_PROFILE,
        tuple(
            results.VerifiedJob(
                cell,
                producers[cell]["job_id"],
                producers[cell]["actual_attempt"],
                "passed",
                results.REQUIRED_STEPS[cell.category],
            )
            for cell in expected.selected
        ),
        inventory(files),
    )


@pytest.fixture
def bundle(tmp_path):
    files = {
        path: value.encode()
        for path, value in json.loads((FIXTURES / "files.json").read_bytes()).items()
    }
    for path, value in files.items():
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value)
    expected = context()
    return tmp_path, expected, acquisition(expected, files), files


def normalize(bundle):
    root, expected, provenance, _files = bundle
    return results.normalize_results(root, expected=expected, provenance=provenance)


def for_attempt(bundle, attempt):
    root, _expected, _provenance, files = bundle
    expected = context(attempt)
    return root, expected, acquisition(expected, files), files


def mutate(bundle, path, keypath, replacement):
    bundle = for_attempt(bundle, 2 if path.startswith(APP) else 1)
    root, expected, provenance, files = bundle
    value = json.loads(files[path])
    target = value
    for key in keypath[:-1]:
        target = target[key]
    target[keypath[-1]] = replacement
    files[path] = results.canonical(value)
    (root / path).write_bytes(files[path])
    return root, expected, replace(provenance, files=inventory(files)), files


@pytest.mark.parametrize("attempt,count", [(1, 7), (2, 1)])
def test_actual_attempt_observations_normalize_without_claiming_complete_repository_acceptance(
    bundle, attempt, count
):
    bundle = for_attempt(bundle, attempt)
    document = normalize(bundle)
    assert document["test-results-version"] == 1
    assert len(document["results"]) == count
    assert document["attempt"] == attempt
    assert all(
        cell["outcome"] == "passed" and results.sha(cell["evidence_sha256"])
        for cell in document["results"]
    )
    assert document["policy_sha256"] == bundle[1].policy_sha256
    assert "complete_application_gate" not in document
    assert "complete_upgrade_gate" not in document


@pytest.mark.parametrize("layer", ["core", "workflows"])
def test_authenticated_after_receipt_cannot_reuse_before_execution_evidence(bundle, layer):
    bundle = for_attempt(bundle, 2)
    root, expected, provenance, files = bundle
    before = APP + f"application-evidence/before-{layer}.json"
    after = APP + f"application-evidence/after-{layer}.json"
    files[after] = files[before]
    (root / after).write_bytes(files[after])
    # Reauthenticate the changed extracted bytes: rejection must come from
    # contradictory generation facts, not the separate file-digest check.
    provenance = replace(provenance, files=inventory(files))
    with pytest.raises(results.NormalizationError, match="cold generation reused"):
        normalize((root, expected, provenance, files))


@pytest.mark.parametrize("attempt,count", [(1, 7), (2, 1)])
def test_normalization_does_not_call_execution_or_network_adapters(
    bundle, monkeypatch, attempt, count
):
    bundle = for_attempt(bundle, attempt)

    def forbidden(*_args, **_kwargs):
        pytest.fail("offline normalization attempted execution or network access")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    assert len(normalize(bundle)["results"]) == count


@pytest.mark.parametrize("attempt,index", [(1, index) for index in range(7)] + [(2, 0)])
def test_each_selected_cell_is_independent_and_digests_are_order_independent(
    bundle, attempt, index
):
    bundle = for_attempt(bundle, attempt)
    root, expected, provenance, files = bundle
    all_results = normalize(bundle)["results"]
    selected = (expected.selected[index],)
    expected = replace(expected, selected=selected)
    provenance = replace(provenance, jobs=(provenance.jobs[index],))
    single = normalize((root, expected, provenance, files))["results"]
    assert single == [all_results[index]]


MUTATIONS = [
    (APP + "application-evidence/producers.json", (0, "uid"), ""),
    (APP + "application-evidence/producers.json", (0, "pod"), "unrelated-producer"),
    (APP + "application-evidence/summary.json", ("namespace_removed",), False),
    (
        APP + "application-evidence/summary.json",
        ("qualification_profile", "validation_intent"),
        "diagnostic",
    ),
    (
        APP + "application-evidence/summary.json",
        ("worker_images", "modes", "ml", "inactive_lease"),
        False,
    ),
    (APP + "application-evidence/summary.json", ("complete_application_gate",), True),
    (APP + "application-evidence/after-nodes.json", ("previous_receipt_sha256",), "0" * 64),
    (APP + "application-evidence/after-core.json", ("status",), "failed"),
    (APP + "application-evidence/after-rq2.json", ("generation",), "before"),
    (APP + "application-evidence/before-protocol.json", ("schema_version",), True),
    (APP + "application-evidence/producers.json", (0, "image_id"), "example@sha256:" + "a" * 64),
    (NATIVE, ("backends", 0, "fixture_cleanup"), False),
    (NATIVE, ("backends", 1, "phases"), []),
    (NATIVE, ("backends", 0, "phases", 0, "observations", "active_leases"), 1),
    (NATIVE, ("backends", 0, "phases", 0, "observations", "active_leases"), False),
    (NATIVE, ("backends", 1, "phases", 4, "observations", "migrations_retained"), False),
    (
        NATIVE,
        (
            "backends",
            0,
            "phases",
            2,
            "observations",
            "retired_carriers",
            "application_boundary_calls",
        ),
        1,
    ),
    (
        NATIVE,
        (
            "backends",
            0,
            "phases",
            3,
            "observations",
            "completion_encoding_failure",
            "application_invocations",
        ),
        2,
    ),
    (
        NATIVE,
        (
            "backends",
            0,
            "phases",
            3,
            "observations",
            "worker_modes",
            "modes",
            "sync",
            "graceful_exit",
        ),
        False,
    ),
    (NATIVE, ("baseline_archive_sha256",), "0" * 64),
    (NATIVE, ("identities", "candidate", "package", "installed_package_tree_sha256"), "0" * 64),
    (
        NATIVE.replace("-native/", "-jobs-crash/"),
        (
            "backends",
            0,
            "phases",
            3,
            "observations",
            "manager_crash",
            "same_job_attempt_generation",
        ),
        False,
    ),
    (
        NATIVE.replace("-native/", "-core-loss/"),
        ("backends", 0, "phases", 3, "observations", "manager_crash", "old_ray_children_retired"),
        False,
    ),
    (
        NATIVE.replace("-native/", "-jobs/"),
        ("backends", 0, "phases", 5, "observations", "automatic_retries"),
        1,
    ),
    (LATENCY, ("fixture_cleanup",), False),
    (LATENCY, ("observations", "ray_shutdown"), False),
    (LATENCY, ("observations", "cases"), []),
    (CANCEL, ("server_stopped",), False),
    (CANCEL, ("cleanup_failed",), True),
    (CANCEL, ("observations", "facts", "original_driver_exited"), False),
    (CANCEL, ("observations", "facts", "same_request_attempt_generation"), False),
    (MONITOR + "monitoring-evidence/summary.json", ("namespace_removed",), False),
    (MONITOR + "monitoring-evidence/summary.json", ("source_tree",), "0" * 40),
    (MONITOR + "monitoring-evidence/summary.json", ("http", "metrics_auth"), "shared"),
    (MONITOR + "monitoring-evidence/summary.json", ("after_replay", "targets", "ray-workers"), 0),
    (MONITOR + "monitoring-evidence/summary.json", ("replacement", "imported_absent"), False),
    (MONITOR + "monitoring-evidence/summary.json", ("ray_auth", "gcs_anonymous_denied"), False),
    (MONITOR + "monitoring-evidence/summary.json", ("browser", "numeric_query_values"), 0),
    (MONITOR + "monitoring-evidence/summary.json", ("browser", "cleanup_confirmed"), False),
    (
        MONITOR + "monitoring-evidence/summary.json",
        ("containers", 0, "image"),
        "other@sha256:" + "a" * 64,
    ),
    (MONITOR + "monitoring-evidence/summary.json", ("containers", 0, "restarts"), False),
    (MONITOR + "monitoring-evidence/summary.json", ("operator", "ready"), False),
    (MONITOR + "monitoring-evidence/summary.json", ("live_resources", "cpu_millicores"), 2751),
    (MONITOR + "monitoring-evidence/infrastructure-cleanup.json", ("registry_removed",), False),
]


@pytest.mark.parametrize("path,keypath,replacement", MUTATIONS)
def test_reauthenticated_damaged_custom_assertions_are_operational_errors(
    bundle, path, keypath, replacement
):
    with pytest.raises(results.NormalizationError):
        normalize(mutate(bundle, path, keypath, replacement))


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_commit", "a" * 40),
        ("execution_tree", "b" * 40),
        ("run_id", "other"),
        ("attempt", 2),
        ("attempt", True),
        ("contract_profile", "future-0.7-contract"),
        ("contract_profile", "django-ray-e4f1ccc-receipts-v1"),
    ],
)
def test_wrong_independent_provenance_rejected(bundle, field, value):
    root, expected, provenance, files = bundle
    with pytest.raises(results.NormalizationError):
        normalize((root, expected, replace(provenance, **{field: value}), files))


@pytest.mark.parametrize(
    "field,value",
    [
        ("policy_sha256", "A" * 64),
        ("bindings_sha256", None),
        ("plan_digest", "a"),
        ("run_id", "../other"),
        ("attempt", True),
        ("execution_commit", "a" * 64),
    ],
)
def test_malformed_expected_context_rejected(bundle, field, value):
    root, expected, provenance, files = bundle
    with pytest.raises(results.NormalizationError):
        normalize((root, replace(expected, **{field: value}), provenance, files))


@pytest.mark.parametrize("outcome", ["failed", "pending", "cancelled", "blocked"])
def test_nonpassed_job_keeps_outcome_without_consuming_or_fabricating_receipts(bundle, outcome):
    root, expected, provenance, _files = bundle
    expected = replace(expected, selected=expected.selected[:1])
    job = replace(provenance.jobs[0], outcome=outcome, successful_steps=())
    provenance = replace(provenance, jobs=(job,), files=())
    document = normalize((root, expected, provenance, {}))
    assert document["results"][0]["outcome"] == outcome


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "duplicate-id", "no-cleanup"])
def test_job_inventory_cannot_select_greenest_or_omit_cleanup(bundle, change):
    root, expected, provenance, files = bundle
    jobs = provenance.jobs
    if change == "missing":
        jobs = jobs[1:]
    elif change == "duplicate":
        jobs = (jobs[1],) + jobs[1:]
    elif change == "extra":
        jobs = jobs + (jobs[0],)
    elif change == "duplicate-id":
        jobs = (replace(jobs[0], job_id=jobs[1].job_id),) + jobs[1:]
    else:
        jobs = (replace(jobs[0], successful_steps=jobs[0].successful_steps[:-1]),) + jobs[1:]
    with pytest.raises(results.NormalizationError):
        normalize((root, expected, replace(provenance, jobs=jobs), files))


def test_modified_bytes_rejected_even_if_receipt_still_says_passed(bundle):
    bundle = for_attempt(bundle, 2)
    root, _expected, _provenance, files = bundle
    path = APP + "application-evidence/summary.json"
    (root / path).write_bytes(files[path] + b" ")
    with pytest.raises(results.NormalizationError, match="changed-evidence"):
        normalize(bundle)


def test_missing_success_evidence_is_not_normalized_as_failed(bundle):
    bundle = for_attempt(bundle, 2)
    root, _expected, _provenance, _files = bundle
    (root / (APP + "application-evidence/summary.json")).unlink()
    with pytest.raises(results.NormalizationError, match="unavailable-evidence"):
        normalize(bundle)


@pytest.mark.parametrize(
    "field,value", [("outcome", []), ("job_id", True), ("successful_steps", None)]
)
def test_malformed_job_fields_raise_fixed_operational_error(bundle, field, value):
    root, expected, provenance, files = bundle
    jobs = (replace(provenance.jobs[0], **{field: value}),) + provenance.jobs[1:]
    with pytest.raises(results.NormalizationError):
        normalize((root, expected, replace(provenance, jobs=jobs), files))


def test_source_validator_runtime_error_is_normalized(bundle):
    damaged = mutate(bundle, LATENCY, ("observations", "managers_stopped"), False)
    with pytest.raises(results.NormalizationError, match="invalid-custom-acceptance"):
        normalize(damaged)


def test_untrusted_source_error_details_are_absent_from_default_traceback(bundle, monkeypatch):
    from qualification.docker.scenario import QualificationError
    from scripts import test_planning_receipts

    untrusted = "private-artifact-value-should-not-be-logged"

    def rejected(*_args):
        raise QualificationError(untrusted)

    monkeypatch.setattr(test_planning_receipts, "validate_cell", rejected)
    with pytest.raises(results.NormalizationError) as caught:
        normalize(bundle)
    assert str(caught.value) == "invalid-custom-acceptance"
    assert untrusted not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "prefix",
    [
        APP + "application-build/source",
        MONITOR + "monitoring-build/source",
        "upgrade-native-qualification-native/upgrade-source",
        "job-latency-qualification/latency-source",
        "cancellation-qualification/cancellation-source",
    ],
)
def test_authentic_job_cannot_normalize_another_artifact_source(bundle, prefix):
    bundle = for_attempt(bundle, 2 if prefix.startswith(APP) else 1)
    root, expected, provenance, files = bundle
    path = prefix + ".txt"
    files[path] = ("a" * 40 + "\n" + expected.execution_tree + "\n").encode()
    (root / path).write_bytes(files[path])
    with pytest.raises(results.NormalizationError, match="wrong-artifact-source"):
        normalize((root, expected, replace(provenance, files=inventory(files)), files))


def test_json_unicode_and_duplicate_inventory_are_rejected(tmp_path):
    with pytest.raises(results.NormalizationError, match="invalid-json-unicode"):
        results._json(b'{"value":"\\ud800"}')
    item = results.VerifiedFile("file", 0, "a" * 64)
    with pytest.raises(results.NormalizationError):
        results.Evidence(tmp_path, (item, item))


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO boundary")
def test_fifo_is_rejected_without_opening(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    evidence = results.Evidence(tmp_path, inventory({"pipe": b""}))
    with pytest.raises(results.NormalizationError, match="nonregular-evidence"):
        evidence.read("pipe")


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":1e999}',
        b'{"a":' + b"9" * 100 + b"}",
        b"\xff",
        b"[" * 34 + b"0" + b"]" * 34,
    ],
)
def test_strict_json_rejects_malformed_or_unbounded_input(raw):
    with pytest.raises(results.NormalizationError):
        results._json(raw)


@pytest.mark.parametrize(
    "path", ["../outside", "/absolute", "a//b", "a\\b", "a:b", "\ud800", "a" * 1025]
)
def test_unsafe_inventory_paths_rejected(tmp_path, path):
    with pytest.raises(results.NormalizationError):
        results.Evidence(tmp_path, (results.VerifiedFile(path, 0, "a" * 64),))


def test_shared_read_is_present_in_each_cell_evidence_access_set(tmp_path):
    raw = b"shared"
    (tmp_path / "shared").write_bytes(raw)
    evidence = results.Evidence(tmp_path, inventory({"shared": raw}))
    evidence.read("shared")
    evidence.accessed.clear()
    evidence.read("shared")
    assert evidence.accessed == {"shared"}
    assert evidence.total == len(raw)


def test_file_replacement_between_lstat_and_open_rejected_before_read(tmp_path, monkeypatch):
    path = tmp_path / "file"
    path.write_bytes(b"good")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"evil")
    original_open = os.open

    def swapped(target, flags):
        replacement.replace(path)
        return original_open(target, flags)

    monkeypatch.setattr(os, "open", swapped)
    evidence = results.Evidence(tmp_path, inventory({"file": b"good"}))
    with pytest.raises(results.NormalizationError, match="changed-evidence-path"):
        evidence.read("file")


def test_file_and_aggregate_budgets(tmp_path, monkeypatch):
    with pytest.raises(results.NormalizationError):
        results.Evidence(
            tmp_path, (results.VerifiedFile("file", results.MAX_FILE_BYTES + 1, "a" * 64),)
        )
    (tmp_path / "file").write_bytes(b"data")
    monkeypatch.setattr(results, "MAX_TOTAL_BYTES", 3)
    evidence = results.Evidence(tmp_path, inventory({"file": b"data"}))
    with pytest.raises(results.NormalizationError, match="evidence-byte-budget"):
        evidence.read("file")


def test_fixture_context_has_exact_historical_source_and_no_live_provenance():
    assert context().execution_commit == "85da4256b04112454228d301f2c81a959876bff4"
    assert context(2).execution_commit == context(1).execution_commit
    assert context(1).execution_tree == "ed59ad1e05fad81ecdf660abc5a71e34ce30d9c2"
    assert results.CONTRACT_PROFILE == "django-ray-45180ea-receipts-v1"
    assert set(context(1).selected).isdisjoint(context(2).selected)
    assert len(context(1).selected) + len(context(2).selected) == 8
    assert context(1).plan_digest != context(2).plan_digest
    assert "jobs" not in asdict(context())


def test_copied_success_rows_cannot_create_a_green_full_plan_for_attempt_two(bundle):
    root, _expected, _provenance, files = bundle
    expected = replace(context(2), selected=context(1).selected + context(2).selected)
    provenance = acquisition(expected, files)
    assert provenance.attempt == 2
    assert {job.attempt for job in provenance.jobs} == {1, 2}
    with pytest.raises(results.NormalizationError, match="wrong-job-execution-attempt"):
        results.normalize_results(root, expected=expected, provenance=provenance)


@pytest.mark.parametrize("attempt", [None, True, False, "1", 0, 2, 2147483648])
def test_job_attempt_must_be_exact_actual_integer_not_parent_retry(bundle, attempt):
    root, expected, provenance, files = bundle
    job = replace(provenance.jobs[0], attempt=attempt)
    with pytest.raises(results.NormalizationError, match="wrong-job-execution-attempt"):
        normalize((root, expected, replace(provenance, jobs=(job, *provenance.jobs[1:])), files))


def test_matching_tree_cannot_relabel_receipts_to_reviewed_main_contract_source(bundle):
    root, expected, provenance, files = bundle
    main = "45180ea0e47aab600ed5b77312b636c7dfdb5282"
    expected = replace(expected, execution_commit=main)
    with pytest.raises(results.NormalizationError, match="wrong-provenance-identity"):
        normalize((root, expected, provenance, files))
    provenance = replace(provenance, execution_commit=main)
    with pytest.raises(results.NormalizationError, match="wrong-artifact-source"):
        normalize((root, expected, provenance, files))


def test_failed_attempt_and_retry_remain_historical_provenance():
    fixture = fixture_context()
    assert fixture["observations"][0]["parent_conclusion"] == "failure"
    assert fixture["history"]["failed_application"]["attempt"] == 1
    assert fixture["history"]["failed_application"]["outcome"] == "failed"
    assert fixture["history"]["aggregate"]["attempt"] == 2
    assert fixture["history"]["retry_is_fix"] is False
    assert fixture["history"]["copied_success_rows_are_execution"] is False
    assert fixture["history"]["cross_attempt_yaga_assembly_supported"] is False
