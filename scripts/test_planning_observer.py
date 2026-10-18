"""Bounded read-only observers publish actual advisory result artifacts."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path

from scripts import test_planning_acquisition as acquisition
from scripts import test_planning_experiment as experiment
from scripts import test_planning_pilot as pilot_tools
from scripts import test_planning_results as normalization
from scripts import test_planning_shadow as shadow

POLL_SECONDS = 60
MAX_POLLS = 260
MAX_WAIT_SECONDS = 260 * 60


def expected_run(pilot: dict, arm: str, run_id: str, attempt: int):
    arm_plan = pilot["arms"][arm]
    return normalization.ExpectedRun(
        plan_digest=arm_plan["plan_digest"],
        policy_sha256=arm_plan["policy_sha256"],
        bindings_sha256=pilot["bindings_sha256"],
        execution_commit=pilot["source"]["execution_commit"],
        execution_tree=pilot["source"]["execution_tree"],
        run_id=run_id,
        attempt=attempt,
        selected=tuple(normalization.Cell(**cell) for cell in arm_plan["selected"]),
    )


def wait_for_receipts(expected, destination, *, acquire=None, clock=None, sleep=None):
    """Finite polling changes no workflow state and never retries failed evidence."""
    acquire = acquire or acquisition.acquire_receipts
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + MAX_WAIT_SECONDS
    for index in range(MAX_POLLS):
        if clock() >= deadline:
            break
        try:
            acquired = acquire(expected, destination)
        except acquisition.AcquisitionPending:
            remaining = deadline - clock()
            if remaining <= 0 or index + 1 == MAX_POLLS:
                break
            sleep(min(POLL_SECONDS, remaining))
        else:
            if clock() >= deadline:
                break
            return acquired
    raise acquisition.AcquisitionError("shadow-observation-deadline")


def make_observation(pilot, arm, expected, results, evaluation, raw_evaluation, evidence):
    """Only authenticated job durations are counted; publication time comes later."""
    jobs = {
        (job["cell"]["suite"], job["cell"]["variant"]): job for job in evidence["selected_jobs"]
    }
    if len(jobs) != len(evidence["selected_jobs"]):
        raise shadow.ShadowError("Duplicate acquired observation job")
    cells = []
    for result in results["results"]:
        job = jobs[(result["suite"], result["variant"])]
        if (
            job["outcome"] != result["outcome"]
            or job["cell"]["binding"] != result["binding"]
            or type(job["actual_attempt"]) is not int
            or job["actual_attempt"] != expected.attempt
        ):
            raise shadow.ShadowError("Acquired observation differs from the evaluated result")
        cells.append(
            {
                "suite": result["suite"],
                "variant": result["variant"],
                "outcome": result["outcome"],
                "evidence_sha256": result.get("evidence_sha256"),
                "attempts": [
                    {
                        "job_id": str(job["job_id"]),
                        "run_id": expected.run_id,
                        "attempt": job["actual_attempt"],
                        "retry_ordinal": 1,
                        "outcome": result["outcome"],
                        "runner_seconds": job["duration_seconds"],
                    }
                ],
            }
        )
    source = pilot["source"]
    # Every arm uses the same range identities, including the full failure oracle.
    identities = {
        "source": source,
        "adapter_source": {
            "execution_commit": pilot["adapter_source"]["commit"],
            "execution_tree": pilot["adapter_source"]["tree"],
        },
        "policy_sha256": expected.policy_sha256,
        "bindings_sha256": expected.bindings_sha256,
        "plan_digest": expected.plan_digest,
        "results_sha256": evaluation["results_sha256"],
        "evaluation_sha256": hashlib.sha256(raw_evaluation).hexdigest(),
        "run_id": expected.run_id,
        "attempt": expected.attempt,
    }
    timing = dict.fromkeys(experiment.TIMES)
    timing["workflow_created_at"] = evidence["workflow_created_at"]
    failures = [job["completed_at"] for job in jobs.values() if job["outcome"] == "failed"]
    timing["first_failure_at"] = min(failures, key=experiment._timestamp) if failures else None
    measurements = {
        "experiment_id": "shadow-" + expected.run_id,
        "arm": arm,
        "fingerprints": dict.fromkeys(
            ("environment_sha256", "images_sha256", "dependencies_sha256")
        ),
        "cache_state": "unknown",
        "timing": timing,
        "cells": cells,
        "artifacts": [
            {"name": "artifact-" + str(item["id"]), "sha256": item["sha256"]}
            for item in evidence["artifacts"]
        ],
        "incomplete_reason": (
            "Selected qualification jobs did not reach a complete result."
            if any(
                cell["outcome"] in {"cancelled", "blocked", "pending", "missing"} for cell in cells
            )
            else None
        ),
    }
    return experiment.build_observation(identities, measurements)


def evaluate_group(
    directory: Path, output: Path, scratch: Path, group: str, run_id: str, attempt: int
):
    """Authenticate real receipts, then call YAGA's installed pure evaluator."""
    if type(attempt) is not int or attempt != 1:
        raise acquisition.AcquisitionError("fresh-shadow-run-required")
    pilot = shadow.decode(shadow.read_bounded(directory / "pilot.json"))
    if group not in pilot["groups"] or group not in pilot_tools.ARMS:
        raise shadow.ShadowError("Unknown shadow publication group")
    expected = expected_run(pilot, group, run_id, attempt)
    aliases = pilot["groups"][group]
    if (
        type(aliases) is not list
        or not 1 <= len(aliases) <= len(pilot_tools.ARMS)
        or any(type(arm) is not str or arm not in pilot_tools.ARMS for arm in aliases)
        or len(set(aliases)) != len(aliases)
        or group not in aliases
    ):
        raise shadow.ShadowError("Invalid shadow publication aliases")
    obligations = set(expected.selected)
    for arm in aliases:
        arm_cells = expected_run(pilot, arm, run_id, attempt).selected
        if len(arm_cells) != len(obligations) or set(arm_cells) != obligations:
            raise shadow.ShadowError("Shadow publication aliases have different obligations")
    acquired = wait_for_receipts(expected, scratch)
    diagnostics = importlib.import_module("yaga.test_planning.diagnostics")
    evaluator = importlib.import_module("yaga.test_planning.evaluation")
    result_parser = importlib.import_module("yaga.test_planning.results")
    exit_code = 0
    for arm in aliases:
        arm_expected = replace(
            expected,
            plan_digest=pilot["arms"][arm]["plan_digest"],
            policy_sha256=pilot["arms"][arm]["policy_sha256"],
        )
        plan_document = shadow.decode(shadow.read_bounded(directory / f"pilot-{arm}-plan.json"))
        planned = diagnostics.parse_plan_document(plan_document)
        document = normalization.normalize_results(
            acquired.root, expected=arm_expected, provenance=acquired.provenance
        )
        results = result_parser.parse_results_document(document)
        evaluated = evaluator.evaluate_results(
            planned,
            results,
            expected_policy_sha256=arm_expected.policy_sha256,
            expected_bindings_sha256=arm_expected.bindings_sha256,
            expected_execution_commit=arm_expected.execution_commit,
            expected_execution_tree=arm_expected.execution_tree,
            expected_run_id=arm_expected.run_id,
            expected_attempt=arm_expected.attempt,
        )
        raw_evaluation = evaluator.encode_evaluation(evaluated)
        observation = make_observation(
            pilot,
            arm,
            arm_expected,
            document,
            json.loads(raw_evaluation),
            raw_evaluation,
            acquired.evidence,
        )
        (output / f"{arm}-plan.json").write_bytes(shadow.encode(plan_document))
        (output / f"{arm}-results.json").write_bytes(result_parser.encode_results(results))
        (output / f"{arm}-evaluation.json").write_bytes(raw_evaluation)
        (output / f"{arm}-observation.json").write_bytes(experiment.encode_observation(observation))
        exit_code = max(exit_code, evaluated.exit_code)
    (output / "acquisition.json").write_bytes(shadow.encode(acquired.evidence))
    (output / "publication.json").write_bytes(
        shadow.encode(
            {
                "test-shadow-publication-version": 1,
                "activation": "disabled",
                "group": group,
                "arms": pilot["groups"][group],
                "run_id": run_id,
                "attempt": attempt,
                "execution_commit": expected.execution_commit,
                "execution_tree": expected.execution_tree,
                "status": "passed" if exit_code == 0 else "failed",
            }
        )
    )
    return exit_code


def regenerate(args):
    documents = shadow.generate(
        args.repo,
        policy_path=args.policy,
        baseline_path=args.baseline_policy,
        revision_range=args.range,
        execution_commit=args.execution_commit,
        execution_tree=args.execution_tree,
        adapter_commit=args.adapter_commit,
        adapter_tree=args.adapter_tree,
        full_shadow=True,
    )
    args.directory.mkdir(parents=True, exist_ok=False)
    for name, document in documents.items():
        (args.directory / name).write_bytes(shadow.encode(document))
    return pilot_tools.prepare(args.directory, args.policy, args.baseline_policy)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "policy", "baseline-policy", "directory", "output", "scratch"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("range", "execution-commit", "execution-tree", "adapter-commit", "adapter-tree"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--group", required=True, choices=pilot_tools.ARMS)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        if (
            args.attempt != 1
            or args.execution_commit != args.adapter_commit
            or args.execution_tree != args.adapter_tree
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        ):
            raise shadow.ShadowError("Fresh manual same-source shadow run required")
        regenerate(args)
        return evaluate_group(
            args.directory, args.output, args.scratch, args.group, args.run_id, args.attempt
        )
    except (RuntimeError, ValueError, TypeError, KeyError, OSError, ImportError, RecursionError):
        (args.output / "error.json").write_bytes(
            shadow.encode(
                {
                    "test-shadow-publication-version": 1,
                    "activation": "disabled",
                    "status": "incomplete",
                    "group": args.group,
                    "run_id": args.run_id,
                    "attempt": args.attempt,
                    "error": "Receipt acquisition, normalization or evaluation did not complete",
                }
            )
        )
        print("Advisory observation incomplete; inspect original job and acquisition evidence")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
