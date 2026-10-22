"""Authenticate advisory publications and measure confirmed publication lead."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import re
from pathlib import Path

from scripts import test_planning_acquisition as acquisition
from scripts import test_planning_experiment as experiment
from scripts import test_planning_feedback as feedback
from scripts import test_planning_observer as observer
from scripts import test_planning_pilot as pilot_tools
from scripts import test_planning_shadow as shadow

UPLOAD_STEP = "Publish validated advisory feedback"
PUBLICATION_TIME_SOURCE = "successful-upload-step-completion"
MAX_OBSERVER_JOB_SECONDS = 31 * 24 * 60 * 60
require = acquisition.require


def verify_jobs(needs, *, full_shadow, execution_commit, execution_tree):
    """Keep the frozen qualification graph and independently require observers."""
    require(type(needs) is dict and "evaluation" in needs, "missing-observer-dependency")
    evaluation = needs["evaluation"]
    expected = "success" if full_shadow else "skipped"
    require(
        type(evaluation) is dict and evaluation.get("result") == expected,
        "unexpected-observer-outcome",
    )
    planning = needs.get("planning")
    require(type(planning) is dict, "missing-planning-dependency")
    original_outputs = planning.get("outputs")
    require(type(original_outputs) is dict, "invalid-planning-outputs")
    outputs = dict(original_outputs)
    matrix_raw = outputs.pop("evaluation_matrix", None)
    require(type(matrix_raw) is str and len(matrix_raw) <= 1024, "invalid-evaluation-matrix")
    matrix = shadow.decode(matrix_raw.encode("utf-8"), 1024)
    require(type(matrix) is dict and set(matrix) == {"include"}, "invalid-evaluation-matrix")
    rows = matrix["include"]
    require(type(rows) is list and 1 <= len(rows) <= 3, "invalid-evaluation-matrix")
    groups = []
    for row in rows:
        require(
            type(row) is dict
            and set(row) == {"group"}
            and type(row["group"]) is str
            and row["group"] in pilot_tools.ARMS,
            "invalid-evaluation-matrix",
        )
        groups.append(row["group"])
    require(len(groups) == len(set(groups)) and "full" in groups, "invalid-evaluation-matrix")
    base_needs = {key: value for key, value in needs.items() if key != "evaluation"}
    base_needs["planning"] = {**planning, "outputs": outputs}
    result = shadow.verify_jobs(
        base_needs,
        full_shadow=full_shadow,
        execution_commit=execution_commit,
        execution_tree=execution_tree,
    )
    result["observer_result"] = expected
    return result


def feedback_expected(pilot, run_id, attempt):
    return {
        "execution_commit": pilot["source"]["execution_commit"],
        "execution_tree": pilot["source"]["execution_tree"],
        "adapter_source": {
            "execution_commit": pilot["adapter_source"]["commit"],
            "execution_tree": pilot["adapter_source"]["tree"],
        },
        "bindings_sha256": pilot["bindings_sha256"],
        "run_id": run_id,
        "attempt": attempt,
        "arms": {
            arm: {
                **pilot["arms"][arm],
                "selected": [
                    {key: cell[key] for key in ("suite", "variant")}
                    for cell in pilot["arms"][arm]["selected"]
                ],
            }
            for arm in pilot_tools.ARMS
        },
    }


def observer_job_seconds(job, *, group, expected, run):
    """Terminal runner duration is independent of publication or upload success."""
    require(acquisition._integer(job.get("id")), "invalid-publication-job-identity")
    for key, wanted in {
        "name": f"Shadow Result Evaluation ({group})",
        "run_id": int(expected.run_id),
        "run_attempt": 1,
        "head_sha": expected.execution_commit,
        "head_branch": "main",
        "workflow_name": acquisition.WORKFLOW_NAME,
        "status": "completed",
    }.items():
        acquisition._same(job.get(key), wanted, "wrong-publication-job-lineage")
    require(
        job.get("conclusion") in {"success", "failure", "cancelled", "timed_out"},
        "incomplete-observer-job",
    )
    start = acquisition._timestamp(job.get("started_at"))
    end = acquisition._timestamp(job.get("completed_at"))
    require(
        acquisition._timestamp(run["run_started_at"]) <= start <= end,
        "invalid-publication-job-window",
    )
    seconds = (end - start).total_seconds()
    require(0 <= seconds <= MAX_OBSERVER_JOB_SECONDS, "observer-job-duration-budget")
    return seconds


def publication_job(job, *, group, expected, run):
    """A failed evaluator may publish an authenticated failed full oracle."""
    observer_job_seconds(job, group=group, expected=expected, run=run)
    require(job.get("conclusion") in {"success", "failure"}, "incomplete-publication-job")
    start = acquisition._timestamp(job["started_at"])
    end = acquisition._timestamp(job["completed_at"])
    steps = job.get("steps")
    require(
        type(steps) is list and len(steps) <= 100 and all(type(step) is dict for step in steps),
        "invalid-publication-steps",
    )
    uploads = [step for step in steps if step.get("name") == UPLOAD_STEP]
    require(len(uploads) == 1, "missing-publication-upload")
    upload = uploads[0]
    require(
        upload.get("status") == "completed" and upload.get("conclusion") == "success",
        "publication-upload-not-successful",
    )
    require(
        start
        <= acquisition._timestamp(upload.get("started_at"))
        <= acquisition._timestamp(upload.get("completed_at"))
        <= end,
        "invalid-publication-upload-window",
    )
    return upload


def validate_bundle(files, *, pilot, group, directory, run_id, attempt):
    """Re-evaluate results against freshly regenerated plans, never downloaded policy."""
    diagnostics = importlib.import_module("yaga.test_planning.diagnostics")
    evaluator = importlib.import_module("yaga.test_planning.evaluation")
    result_parser = importlib.import_module("yaga.test_planning.results")
    publication = shadow.decode(files["publication.json"])
    acquisition_evidence = shadow.decode(files["acquisition.json"])
    observations = {}
    exit_code = 0
    for arm in pilot["groups"][group]:
        expected = observer.expected_run(pilot, arm, run_id, attempt)
        fresh_plan = shadow.read_bounded(directory / f"pilot-{arm}-plan.json")
        require(files[f"{arm}-plan.json"] == fresh_plan, "publication-plan-differs")
        planned = diagnostics.parse_plan_document(shadow.decode(fresh_plan))
        result_document = shadow.decode(files[f"{arm}-results.json"])
        results = result_parser.parse_results_document(result_document)
        evaluated = evaluator.evaluate_results(
            planned,
            results,
            expected_policy_sha256=expected.policy_sha256,
            expected_bindings_sha256=expected.bindings_sha256,
            expected_execution_commit=expected.execution_commit,
            expected_execution_tree=expected.execution_tree,
            expected_run_id=expected.run_id,
            expected_attempt=expected.attempt,
        )
        encoded = evaluator.encode_evaluation(evaluated)
        require(encoded == files[f"{arm}-evaluation.json"], "publication-evaluation-differs")
        rebuilt = observer.make_observation(
            pilot,
            arm,
            expected,
            result_document,
            shadow.decode(encoded),
            encoded,
            acquisition_evidence,
        )
        raw = experiment.encode_observation(rebuilt)
        require(raw == files[f"{arm}-observation.json"], "publication-observation-differs")
        observations[arm] = rebuilt
        exit_code = max(exit_code, evaluated.exit_code)
    acquisition._same(
        publication,
        {
            "test-shadow-publication-version": 1,
            "activation": "disabled",
            "group": group,
            "arms": pilot["groups"][group],
            "run_id": run_id,
            "attempt": attempt,
            "execution_commit": pilot["source"]["execution_commit"],
            "execution_tree": pilot["source"]["execution_tree"],
            "status": "passed" if exit_code == 0 else "failed",
        },
        "publication-envelope-differs",
    )
    return observations, exit_code


def collect(pilot, directory, output, *, run_id, attempt, github=None):
    """Retain original publication archives and authenticated timing evidence."""
    require(
        type(run_id) is str
        and re.fullmatch(r"[1-9][0-9]{0,15}", run_id) is not None
        and type(attempt) is int
        and attempt == 1,
        "fresh-shadow-run-required",
    )
    expected = observer.expected_run(pilot, "full", run_id, attempt)
    client = github if github is not None else acquisition.Github()
    for name in ("archives", "api", "logs", "publications"):
        (output / name).mkdir()
    api = []

    def read(route, label):
        raw = client.get(route, acquisition.MAX_JSON)
        value = shadow.decode(raw, acquisition.MAX_JSON)
        require(type(value) is dict, "invalid-publication-api-object")
        (output / "api" / (label + ".json")).write_bytes(raw)
        api.append({"path": "api/" + label + ".json", "sha256": hashlib.sha256(raw).hexdigest()})
        return value

    route = f"{acquisition.PREFIX}/actions/runs/{run_id}"
    run = read(route, "run-before")
    acquisition.validate_run(run, expected)
    acquisition.validate_run(read(route + "/attempts/1", "attempt-before"), expected)
    jobs = acquisition._items(read(route + "/attempts/1/jobs?per_page=100&page=1", "jobs"), "jobs")
    artifacts = acquisition._items(
        read(route + "/artifacts?per_page=100&page=1", "artifacts"), "artifacts"
    )
    publications = dict.fromkeys(pilot_tools.ARMS)
    records: list[dict] = []
    observer_job_ids: set[int] = set()
    for group, arms in pilot["groups"].items():
        record = {"group": group, "status": "incomplete", "runner_seconds": None}
        records.append(record)
        timing = None
        candidate = None
        try:
            matches = [
                job for job in jobs if job.get("name") == f"Shadow Result Evaluation ({group})"
            ]
            require(len(matches) == 1, "missing-or-duplicate-publication-job")
            job = read(f"{acquisition.PREFIX}/actions/jobs/{matches[0]['id']}", "job-" + group)
            acquisition._same(job.get("id"), matches[0]["id"], "wrong-publication-job-detail")
            seconds = observer_job_seconds(job, group=group, expected=expected, run=run)
            require(job["id"] not in observer_job_ids, "duplicate-observer-job")
            observer_job_ids.add(job["id"])
            timing = (job, seconds)
            upload = publication_job(job, group=group, expected=expected, run=run)
            name = f"test-planning-evaluation-{run_id}-1-{group}"
            matches = [artifact for artifact in artifacts if artifact.get("name") == name]
            require(len(matches) == 1, "missing-or-duplicate-publication-artifact")
            artifact = matches[0]
            acquisition.validate_artifact_metadata(artifact, expected, upload, name)
            logs = client.get(
                f"{acquisition.PREFIX}/actions/jobs/{job['id']}/logs", acquisition.MAX_LOG
            )
            acquisition.validate_upload_log(logs, artifact, upload)
            (output / "logs" / f"{job['id']}.log").write_bytes(logs)
            raw = client.get(
                f"{acquisition.PREFIX}/actions/artifacts/{artifact['id']}/zip", acquisition.MAX_ZIP
            )
            digest = hashlib.sha256(raw).hexdigest()
            require(
                len(raw) == artifact["size_in_bytes"] and "sha256:" + digest == artifact["digest"],
                "publication-archive-differs",
            )
            (output / "archives" / f"{artifact['id']}.zip").write_bytes(raw)
            record.update(
                artifact_id=artifact["id"],
                artifact_sha256=digest,
                artifact_created_at=artifact["created_at"],
                confirmed_publication_at=upload["completed_at"],
                publication_time_source=PUBLICATION_TIME_SOURCE,
            )
            files = acquisition.extract_receipts(
                raw,
                frozenset(
                    {"publication.json", "acquisition.json"}
                    | {
                        f"{arm}-{kind}.json"
                        for arm in arms
                        for kind in ("plan", "results", "evaluation", "observation")
                    }
                ),
                max_file_bytes=shadow.MAX_DOCUMENT_BYTES,
            )
            folder = output / "publications" / group
            folder.mkdir()
            for name, data in files.items():
                (folder / name).write_bytes(data)
            observations, evaluation_exit = validate_bundle(
                files,
                pilot=pilot,
                group=group,
                directory=directory,
                run_id=run_id,
                attempt=attempt,
            )
            require(
                (job["conclusion"] == "success") == (evaluation_exit == 0),
                "publication-job-outcome-differs",
            )
            acquisition._same(
                read(
                    f"{acquisition.PREFIX}/actions/artifacts/{artifact['id']}",
                    "artifact-" + group + "-after",
                ),
                artifact,
                "publication-artifact-changed",
            )
            candidate = (observations, files, artifact, upload, digest, evaluation_exit)
        except (
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            OSError,
            ImportError,
            RecursionError,
        ):
            record["error"] = "Publication missing, incomplete or not authenticated"
        if timing is not None:
            job, seconds = timing
            try:
                acquisition._same(
                    read(
                        f"{acquisition.PREFIX}/actions/jobs/{job['id']}", "job-" + group + "-after"
                    ),
                    job,
                    "publication-job-changed",
                )
                record.update(
                    job_id=job["id"], job_conclusion=job["conclusion"], runner_seconds=seconds
                )
            except (
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                OSError,
                ImportError,
                RecursionError,
            ):
                candidate = None
                record["error"] = "Publication missing, incomplete or not authenticated"
        if candidate is not None:
            observations, files, artifact, upload, digest, evaluation_exit = candidate
            for arm, observation in observations.items():
                publications[arm] = {
                    "observation": observation,
                    "observation_sha256": hashlib.sha256(
                        files[f"{arm}-observation.json"]
                    ).hexdigest(),
                    "publication_id": str(artifact["id"]),
                    "published_at": upload["completed_at"],
                    "publication_time_source": PUBLICATION_TIME_SOURCE,
                    "publication_sha256": digest,
                }
            record["status"] = "passed" if evaluation_exit == 0 else "failed"
    acquisition.validate_run(read(route, "run-after"), expected)
    acquisition.validate_run(read(route + "/attempts/1", "attempt-after"), expected)
    report = feedback.build_feedback_report(feedback_expected(pilot, run_id, attempt), publications)
    (output / "feedback.json").write_bytes(feedback.encode_feedback_report(report))
    (output / "publication-acquisition.json").write_bytes(
        shadow.encode(
            {
                "publication-acquisition-version": 1,
                "run_id": run_id,
                "attempt": attempt,
                "execution_commit": expected.execution_commit,
                "execution_tree": expected.execution_tree,
                "activation": "disabled",
                "groups": records,
                "api": api,
            }
        )
    )
    full = publications["full"]
    complete = all(record["status"] != "incomplete" for record in records)
    known_seconds = [
        record["runner_seconds"] for record in records if record["runner_seconds"] is not None
    ]
    (output / "runtime-costs.json").write_bytes(
        shadow.encode(
            {
                "test-shadow-runtime-costs-version": 1,
                "activation": "disabled",
                "qualification_job_seconds": full["observation"]["metrics"]["runner_job_seconds"]
                if full
                else None,
                "observer_job_seconds": sum(known_seconds)
                if records and len(known_seconds) == len(records)
                else None,
                "known_observer_job_seconds": sum(known_seconds) if known_seconds else None,
                "aggregate_job_seconds": None,
                "billing_seconds": None,
                "selective_execution_savings_seconds": None,
                "note": "Observer duration includes idle polling; aggregate is still running. No billing or selective execution savings inferred.",
            }
        )
    )
    return 0 if complete and report["full_oracle"]["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--full-shadow", action="store_true")
    collect_parser = commands.add_parser("collect")
    for command in (verify, collect_parser):
        for name in ("execution-commit", "execution-tree"):
            command.add_argument("--" + name, required=True)
    for name in ("repo", "policy", "baseline-policy", "directory", "output"):
        collect_parser.add_argument("--" + name, required=True, type=Path)
    for name in ("range", "adapter-commit", "adapter-tree", "run-id"):
        collect_parser.add_argument("--" + name, required=True)
    collect_parser.add_argument("--attempt", required=True, type=int)
    args = parser.parse_args(argv)
    if args.command == "collect":
        args.output.mkdir(parents=True, exist_ok=False)
    try:
        if args.command == "verify":
            document = verify_jobs(
                shadow.decode(os.environ.get("SHADOW_NEEDS", "").encode(), 16384),
                full_shadow=args.full_shadow,
                execution_commit=args.execution_commit,
                execution_tree=args.execution_tree,
            )
            print(shadow.encode(document).decode())
            return 0
        require(
            args.attempt == 1
            and args.execution_commit == args.adapter_commit
            and args.execution_tree == args.adapter_tree
            and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch",
            "fresh-manual-same-source-required",
        )
        pilot = observer.regenerate(args)
        return collect(pilot, args.directory, args.output, run_id=args.run_id, attempt=args.attempt)
    except (RuntimeError, ValueError, TypeError, KeyError, OSError, ImportError, RecursionError):
        document = {
            "test-shadow-publication-version": 1,
            "activation": "disabled",
            "status": "incomplete",
            "error": "Advisory publication validation did not complete",
        }
        if args.command == "collect":
            (args.output / "error.json").write_bytes(shadow.encode(document))
        print(shadow.encode(document).decode())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
