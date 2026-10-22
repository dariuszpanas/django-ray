"""Offline collector tests use synthetic API/ZIP data and a fake evaluator."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from scripts import test_planning_acquisition as acquisition
from scripts import test_planning_experiment as experiment
from scripts import test_planning_feedback as feedback
from scripts import test_planning_observer as observer
from scripts import test_planning_publications as publications
from scripts import test_planning_shadow as shadow
from scripts.test_planning_results import BINDINGS, canonical
from tests.unit.test_test_planning_acquisition import (
    END,
    RUN,
    SOURCE,
    START,
    TREE,
    UPLOAD_START,
    UPLOADED,
    GithubFixture,
    zip_bytes,
)


def needs(full=True):
    return {
        "planning": {
            "result": "success",
            "outputs": {
                "execution_commit": SOURCE,
                "execution_tree": TREE,
                "full_shadow": "true" if full else "false",
                "policy_sha256": "a" * 64,
                "bindings_sha256": "b" * 64,
                "plan_digest": "c" * 64,
                "evaluation_matrix": '{"include":[{"group":"full"}]}',
            },
        },
        **{
            name: {"result": "success" if full else "skipped"}
            for name in (*shadow.JOBS, "evaluation")
        },
    }


@pytest.mark.parametrize("full", [True, False])
def test_verify_wrapper_validates_and_removes_only_matrix_without_mutating_inputs(full):
    value = needs(full)
    before = copy.deepcopy(value)
    report = publications.verify_jobs(
        value, full_shadow=full, execution_commit=SOURCE, execution_tree=TREE
    )
    assert value == before
    assert report["observer_result"] == ("success" if full else "skipped")
    assert set(report["identity"]) == shadow.OUTPUT_KEYS


@pytest.mark.parametrize(
    "matrix",
    [
        None,
        "",
        "{}",
        '{"include":[]}',
        '{"include":[{"group":"current"}]}',
        '{"include":[{"group":"full"},{"group":"full"}]}',
        '{"include":[{"group":"full","execute":true}]}',
        '{"include":[{"group":"other"}]}',
        '{"include":[{"group":true}]}',
        '{"include":[{"group":"full"}],"extra":0}',
        '{"include":[{"group":"full"}],"include":[{"group":"current"}]}',
        "x" * 1025,
    ],
)
def test_aggregate_rejects_missing_unbounded_or_ambiguous_observer_matrix(matrix):
    value = needs()
    value["planning"]["outputs"]["evaluation_matrix"] = matrix
    with pytest.raises((acquisition.AcquisitionError, shadow.ShadowError)):
        publications.verify_jobs(
            value, full_shadow=True, execution_commit=SOURCE, execution_tree=TREE
        )


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped", None])
def test_failed_observer_never_passes_the_manual_aggregate(outcome):
    value = needs()
    value["evaluation"]["result"] = outcome
    with pytest.raises(acquisition.AcquisitionError, match="observer-outcome"):
        publications.verify_jobs(
            value, full_shadow=True, execution_commit=SOURCE, execution_tree=TREE
        )


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    cells = [
        {
            "suite": suite,
            "variant": variant,
            "binding": BINDINGS[suite.removeprefix("qualification.")],
        }
        for suite, variant in sorted(feedback.CATALOG)
    ]
    pilot: dict = {
        "source": {
            "base_commit": None,
            "head_commit": None,
            "comparison_commit": None,
            "execution_commit": SOURCE,
            "execution_tree": TREE,
        },
        "adapter_source": {"commit": SOURCE, "tree": TREE},
        "bindings_sha256": "c" * 64,
        "groups": {"full": ["full", "proposed", "current"]},
        "arms": {
            arm: {
                "policy_sha256": "d" * 64 if arm == "current" else "e" * 64,
                "plan_digest": str(index + 1) * 64,
                "selected": cells,
            }
            for index, arm in enumerate(("current", "proposed", "full"))
        },
    }
    calls = []

    def evaluate(planned, results, **expected):
        calls.append((planned, copy.deepcopy(expected)))
        for key, value in expected.items():
            assert results[key.removeprefix("expected_")] == value
        assert results["plan_digest"] == pilot["arms"][planned["arm"]]["plan_digest"]
        return SimpleNamespace(results=results, exit_code=0)

    def encode(evaluated):
        return shadow.encode(
            {
                "results_sha256": hashlib.sha256(canonical(evaluated.results)).hexdigest(),
                "status": "passed",
            }
        )

    modules = {
        "yaga.test_planning.diagnostics": SimpleNamespace(parse_plan_document=lambda value: value),
        "yaga.test_planning.results": SimpleNamespace(parse_results_document=lambda value: value),
        "yaga.test_planning.evaluation": SimpleNamespace(
            evaluate_results=evaluate, encode_evaluation=encode
        ),
    }
    original_import = publications.importlib.import_module
    monkeypatch.setattr(
        publications.importlib,
        "import_module",
        lambda name: modules[name] if name in modules else original_import(name),
    )
    evidence = {
        "workflow_created_at": START,
        "artifacts": [],
        "selected_jobs": [
            {
                "cell": cell,
                "job_id": 1000 + index,
                "actual_attempt": 1,
                "outcome": "passed",
                "duration_seconds": 120,
                "completed_at": END,
            }
            for index, cell in enumerate(cells)
        ],
    }
    directory = tmp_path / "plans"
    directory.mkdir()
    files = {"acquisition.json": shadow.encode(evidence)}
    for arm in pilot["groups"]["full"]:
        expected = observer.expected_run(pilot, arm, RUN, 1)
        plan = {"arm": arm}
        raw_plan = shadow.encode(plan)
        (directory / f"pilot-{arm}-plan.json").write_bytes(raw_plan)
        results = {
            "test-results-version": 1,
            **{key: value for key, value in asdict(expected).items() if key != "selected"},
            "results": [
                {**cell, "outcome": "passed", "evidence_sha256": str(index + 1) * 64}
                for index, cell in enumerate(cells)
            ],
        }
        encoded = encode(SimpleNamespace(results=results))
        observation = observer.make_observation(
            pilot, arm, expected, results, json.loads(encoded), encoded, evidence
        )
        files.update(
            {
                f"{arm}-plan.json": raw_plan,
                f"{arm}-results.json": shadow.encode(results),
                f"{arm}-evaluation.json": encoded,
                f"{arm}-observation.json": experiment.encode_observation(observation),
            }
        )
    files["publication.json"] = shadow.encode(
        {
            "test-shadow-publication-version": 1,
            "activation": "disabled",
            "group": "full",
            "arms": pilot["groups"]["full"],
            "run_id": RUN,
            "attempt": 1,
            "execution_commit": SOURCE,
            "execution_tree": TREE,
            "status": "passed",
        }
    )
    return SimpleNamespace(pilot=pilot, directory=directory, files=files, calls=calls)


def test_bundle_rebuilds_all_aliases_against_fresh_plans_and_independent_context(bundle):
    observations, code = publications.validate_bundle(
        bundle.files,
        pilot=bundle.pilot,
        group="full",
        directory=bundle.directory,
        run_id=RUN,
        attempt=1,
    )
    assert code == 0 and set(observations) == {"current", "proposed", "full"}
    assert len(bundle.calls) == 3
    assert all(
        expected["expected_run_id"] == RUN and expected["expected_attempt"] == 1
        for _, expected in bundle.calls
    )
    assert all(item["timing"]["feedback_at"] is None for item in observations.values())


@pytest.mark.parametrize("part", ["plan", "evaluation", "observation", "envelope", "acquisition"])
def test_tampered_bundle_cannot_reuse_a_green_observation(bundle, part):
    files = dict(bundle.files)
    if part == "envelope":
        value = shadow.decode(files["publication.json"])
        value["attempt"] = 2
        files["publication.json"] = shadow.encode(value)
    elif part == "acquisition":
        value = shadow.decode(files["acquisition.json"])
        value["selected_jobs"][0]["duration_seconds"] = 121
        files["acquisition.json"] = shadow.encode(value)
    else:
        files[f"full-{part}.json"] += b" "
    with pytest.raises(acquisition.AcquisitionError):
        publications.validate_bundle(
            files,
            pilot=bundle.pilot,
            group="full",
            directory=bundle.directory,
            run_id=RUN,
            attempt=1,
        )


def publication_api(bundle):
    github = GithubFixture(())
    prefix = acquisition.PREFIX
    job_id, artifact_id = 301, 401
    upload = {
        "name": publications.UPLOAD_STEP,
        "status": "completed",
        "conclusion": "success",
        "started_at": UPLOAD_START,
        "completed_at": END,
    }
    job = {
        "id": job_id,
        "name": "Shadow Result Evaluation (full)",
        "run_id": int(RUN),
        "run_attempt": 1,
        "head_sha": SOURCE,
        "head_branch": "main",
        "workflow_name": acquisition.WORKFLOW_NAME,
        "status": "completed",
        "conclusion": "success",
        "started_at": START,
        "completed_at": END,
        "steps": [upload],
    }
    raw = zip_bytes(sorted(bundle.files.items()))
    name = f"test-planning-evaluation-{RUN}-1-full"
    artifact = {
        "id": artifact_id,
        "name": name,
        "expired": False,
        "size_in_bytes": len(raw),
        "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "created_at": UPLOADED,
        "updated_at": UPLOADED,
        "workflow_run": {
            "id": int(RUN),
            "repository_id": acquisition.REPOSITORY_ID,
            "head_repository_id": acquisition.REPOSITORY_ID,
            "head_sha": SOURCE,
            "head_branch": "main",
        },
    }
    logs = f"{UPLOADED} SHA256 digest of uploaded artifact is {artifact['digest'][7:]}\n{UPLOADED} Artifact {name} has been successfully uploaded! Final size is {len(raw)} bytes. Artifact ID is {artifact_id}\n".encode()
    github.jobs.append(job)
    github.artifacts.append(artifact)
    github.replies[github.jobs_route]["total_count"] = 1
    github.replies[github.artifacts_route]["total_count"] = 1
    github.replies.update(
        {
            f"{prefix}/actions/jobs/{job_id}": job,
            f"{prefix}/actions/jobs/{job_id}/logs": logs,
            f"{prefix}/actions/artifacts/{artifact_id}": artifact,
            f"{prefix}/actions/artifacts/{artifact_id}/zip": raw,
        }
    )
    return github, job, artifact, raw


def test_authenticated_api_zip_roundtrip_uses_upload_completion_not_creation(bundle, tmp_path):
    github, _, artifact, raw = publication_api(bundle)
    output = tmp_path / "collected"
    output.mkdir()
    assert (
        publications.collect(
            bundle.pilot, bundle.directory, output, run_id=RUN, attempt=1, github=github
        )
        == 0
    )
    result = json.loads((output / "feedback.json").read_bytes())
    assert (output / "archives/401.zip").read_bytes() == raw
    assert artifact["created_at"] != END
    assert all(
        item["published_at"] == END
        and item["publication_time_source"] == "successful-upload-step-completion"
        for item in result["publications"].values()
    )
    assert all(
        item["validated_publication_lead_seconds"] == 0 for item in result["comparisons"].values()
    )
    provenance = json.loads((output / "publication-acquisition.json").read_bytes())
    assert provenance["groups"][0]["artifact_created_at"] == UPLOADED
    assert provenance["groups"][0]["confirmed_publication_at"] == END
    costs = json.loads((output / "runtime-costs.json").read_bytes())
    assert costs["qualification_job_seconds"] == 960
    assert costs["observer_job_seconds"] == 600
    assert costs["known_observer_job_seconds"] == 600
    assert costs["selective_execution_savings_seconds"] is costs["billing_seconds"] is None


@pytest.mark.parametrize("diagnostic_status", ["passed", "incomplete"])
def test_extra_diagnostics_are_retained_without_changing_publication_authority(
    bundle, tmp_path, diagnostic_status
):
    bundle.files["diagnostics.json"] = shadow.encode(
        {"status": diagnostic_status, "error": "Diagnostic information only"}
    )
    github, _, _, raw = publication_api(bundle)
    output = tmp_path / "collected"
    output.mkdir()

    assert (
        publications.collect(
            bundle.pilot, bundle.directory, output, run_id=RUN, attempt=1, github=github
        )
        == 0
    )

    result = json.loads((output / "feedback.json").read_bytes())
    assert all(item is not None for item in result["publications"].values())
    assert len(bundle.calls) == 3
    assert (output / "archives/401.zip").read_bytes() == raw
    assert not (output / "publications/full/diagnostics.json").exists()


def test_diagnostic_success_cannot_replace_a_missing_required_publication(bundle, tmp_path):
    bundle.files["diagnostics.json"] = bundle.files.pop("publication.json")
    github, _, _, raw = publication_api(bundle)
    output = tmp_path / "collected"
    output.mkdir()

    assert (
        publications.collect(
            bundle.pilot, bundle.directory, output, run_id=RUN, attempt=1, github=github
        )
        == 1
    )

    result = json.loads((output / "feedback.json").read_bytes())
    assert result["full_oracle"]["status"] == "unavailable"
    assert set(result["publications"].values()) == {None}
    assert all(
        item["validated_publication_lead_seconds"] is None
        for item in result["comparisons"].values()
    )
    assert bundle.calls == []
    assert (output / "archives/401.zip").read_bytes() == raw
    costs = json.loads((output / "runtime-costs.json").read_bytes())
    assert costs["observer_job_seconds"] == costs["known_observer_job_seconds"] == 600
    assert costs["qualification_job_seconds"] is None


@pytest.mark.parametrize(
    "damage",
    [
        "attempt",
        "source",
        "upload-failed",
        "digest",
        "log",
        "changed-job",
        "changed-artifact",
        "missing-job",
    ],
)
def test_invalid_publication_is_retained_as_incomplete_without_feedback_success(
    bundle, tmp_path, damage
):
    github, job, artifact, raw = publication_api(bundle)
    prefix = acquisition.PREFIX
    if damage == "attempt":
        job["run_attempt"] = 2
    elif damage == "source":
        artifact["workflow_run"]["head_sha"] = "f" * 40
    elif damage == "upload-failed":
        job["steps"][0]["conclusion"] = "failure"
    elif damage == "digest":
        github.replies[f"{prefix}/actions/artifacts/401/zip"] = raw[:-1] + b"x"
    elif damage == "log":
        github.replies[f"{prefix}/actions/jobs/301/logs"] = b"uploaded"
    elif damage in {"changed-job", "changed-artifact"}:
        original = job if damage == "changed-job" else artifact
        changed = {**original, "unexpected-change": True}
        answers = iter((original, changed)) if damage == "changed-job" else iter((changed,))
        route = (
            f"{prefix}/actions/jobs/301"
            if damage == "changed-job"
            else f"{prefix}/actions/artifacts/401"
        )
        github.replies[route] = lambda: next(answers)
    else:
        github.jobs.clear()
        github.replies[github.jobs_route]["total_count"] = 0
    output = tmp_path / "collected"
    output.mkdir()
    assert (
        publications.collect(
            bundle.pilot, bundle.directory, output, run_id=RUN, attempt=1, github=github
        )
        == 1
    )
    result = json.loads((output / "feedback.json").read_bytes())
    assert result["full_oracle"]["status"] == "unavailable"
    assert set(result["publications"].values()) == {None}
    assert all(
        item["validated_publication_lead_seconds"] is None
        for item in result["comparisons"].values()
    )
    costs = json.loads((output / "runtime-costs.json").read_bytes())
    duration = None if damage in {"attempt", "changed-job", "missing-job"} else 600
    assert costs["observer_job_seconds"] == costs["known_observer_job_seconds"] == duration


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_authenticated_terminal_failure_duration_survives_a_missing_publication(
    bundle, tmp_path, conclusion
):
    github, job, *_ = publication_api(bundle)
    job["conclusion"] = conclusion
    job["steps"] = []
    output = tmp_path / "collected"
    output.mkdir()
    assert (
        publications.collect(
            bundle.pilot, bundle.directory, output, run_id=RUN, attempt=1, github=github
        )
        == 1
    )
    costs = json.loads((output / "runtime-costs.json").read_bytes())
    assert costs["observer_job_seconds"] == costs["known_observer_job_seconds"] == 600
    assert costs["qualification_job_seconds"] is costs["billing_seconds"] is None
    assert costs["selective_execution_savings_seconds"] is None
    records = json.loads((output / "publication-acquisition.json").read_bytes())["groups"]
    assert records[0]["job_id"] == job["id"]
    assert records[0]["job_conclusion"] == conclusion and records[0]["runner_seconds"] == 600
    assert records[0]["status"] == "incomplete"
    assert github.calls.count(acquisition.PREFIX + "/actions/jobs/301") == 2
    assert set(json.loads((output / "feedback.json").read_bytes())["publications"].values()) == {
        None
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", 999),
        ("run_attempt", 2),
        ("head_sha", "f" * 40),
        ("head_branch", "other"),
        ("workflow_name", "other"),
        ("name", "other"),
        ("status", "in_progress"),
        ("conclusion", "skipped"),
        ("started_at", None),
        ("completed_at", None),
        ("started_at", "2026-10-18T09:59:59Z"),
        ("completed_at", "2026-10-18T09:59:59Z"),
        ("completed_at", "2026-11-18T10:00:01Z"),
    ],
)
def test_untrusted_or_unfinished_job_timing_stays_unknown_without_inventing_zero(
    bundle, tmp_path, field, value
):
    github, job, *_ = publication_api(bundle)
    job[field] = value
    output = tmp_path / "collected"
    output.mkdir()
    assert (
        publications.collect(
            bundle.pilot, bundle.directory, output, run_id=RUN, attempt=1, github=github
        )
        == 1
    )
    costs = json.loads((output / "runtime-costs.json").read_bytes())
    assert costs["observer_job_seconds"] is costs["known_observer_job_seconds"] is None
    records = json.loads((output / "publication-acquisition.json").read_bytes())["groups"]
    assert records[0]["runner_seconds"] is None
    assert records[0]["status"] == "incomplete"


def test_partial_observer_subtotal_does_not_turn_a_missing_group_into_zero(bundle, tmp_path):
    bundle.pilot["groups"] = {"full": ["full", "proposed"], "current": ["current"]}
    github, job, *_ = publication_api(bundle)
    job["steps"] = []
    output = tmp_path / "collected"
    output.mkdir()
    assert (
        publications.collect(
            bundle.pilot, bundle.directory, output, run_id=RUN, attempt=1, github=github
        )
        == 1
    )
    costs = json.loads((output / "runtime-costs.json").read_bytes())
    assert costs["observer_job_seconds"] is None
    assert costs["known_observer_job_seconds"] == 600
    records = json.loads((output / "publication-acquisition.json").read_bytes())["groups"]
    assert [record["runner_seconds"] for record in records] == [600, None]


def test_all_known_observer_groups_are_counted_once_despite_failed_publications(bundle, tmp_path):
    bundle.pilot["groups"] = {"full": ["full", "proposed"], "current": ["current"]}
    github, job, *_ = publication_api(bundle)
    job["steps"] = []
    second = {
        **job,
        "id": 302,
        "name": "Shadow Result Evaluation (current)",
        "conclusion": "failure",
        "completed_at": "2026-10-18T10:00:11Z",
    }
    github.jobs.append(second)
    github.replies[github.jobs_route]["total_count"] = 2
    github.replies[acquisition.PREFIX + "/actions/jobs/302"] = second
    output = tmp_path / "collected"
    output.mkdir()
    assert (
        publications.collect(
            bundle.pilot, bundle.directory, output, run_id=RUN, attempt=1, github=github
        )
        == 1
    )
    costs = json.loads((output / "runtime-costs.json").read_bytes())
    assert costs["observer_job_seconds"] == costs["known_observer_job_seconds"] == 611
    records = json.loads((output / "publication-acquisition.json").read_bytes())["groups"]
    assert [record["runner_seconds"] for record in records] == [600, 11]
    assert all(record["status"] == "incomplete" for record in records)


@pytest.mark.parametrize("failure", ["changed", "unavailable"])
def test_failed_publication_still_requires_a_stable_final_job_read(bundle, tmp_path, failure):
    github, job, *_ = publication_api(bundle)
    job["steps"] = []
    route = acquisition.PREFIX + "/actions/jobs/301"

    def response():
        if github.calls.count(route) == 1:
            return job
        if failure == "changed":
            return {**job, "completed_at": "2026-10-18T10:11:00Z"}
        raise acquisition.AcquisitionError("github-request-failed")

    github.replies[route] = response
    output = tmp_path / "collected"
    output.mkdir()
    assert (
        publications.collect(
            bundle.pilot, bundle.directory, output, run_id=RUN, attempt=1, github=github
        )
        == 1
    )
    costs = json.loads((output / "runtime-costs.json").read_bytes())
    assert costs["observer_job_seconds"] is costs["known_observer_job_seconds"] is None
    assert github.calls.count(route) == 2


def test_duplicate_job_inventory_cannot_double_count_observer_costs(bundle, tmp_path):
    github, job, *_ = publication_api(bundle)
    github.jobs.append({**job, "name": "Shadow Result Evaluation (current)"})
    github.replies[github.jobs_route]["total_count"] = 2
    output = tmp_path / "collected"
    output.mkdir()
    with pytest.raises(acquisition.AcquisitionError, match="duplicate-api-identity"):
        publications.collect(
            bundle.pilot, bundle.directory, output, run_id=RUN, attempt=1, github=github
        )
    assert not (output / "runtime-costs.json").exists()


@pytest.mark.parametrize("end,seconds", [(START, 0), ("2026-11-18T10:00:00Z", 31 * 24 * 60 * 60)])
def test_observer_duration_accepts_only_observed_zero_and_bounded_endpoint(bundle, end, seconds):
    github, job, *_ = publication_api(bundle)
    job["completed_at"] = end
    assert (
        publications.observer_job_seconds(
            job,
            group="full",
            expected=observer.expected_run(bundle.pilot, "full", RUN, 1),
            run=github.run,
        )
        == seconds
    )


def test_rerun_is_refused_before_any_api_call_or_output_directory(bundle, tmp_path):
    github, *_ = publication_api(bundle)
    with pytest.raises(acquisition.AcquisitionError, match="fresh-shadow-run"):
        publications.collect(
            bundle.pilot, bundle.directory, tmp_path, run_id=RUN, attempt=2, github=github
        )
    assert github.calls == [] and list(tmp_path.iterdir()) == [bundle.directory]
