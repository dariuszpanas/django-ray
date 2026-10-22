"""Bounded read-only observers publish actual advisory result artifacts."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from scripts import test_planning_acquisition as acquisition
from scripts import test_planning_experiment as experiment
from scripts import test_planning_pilot as pilot_tools
from scripts import test_planning_results as normalization
from scripts import test_planning_shadow as shadow

POLL_SECONDS = 60
MAX_POLLS = 260
MAX_WAIT_SECONDS = 260 * 60
MAX_DIAGNOSTIC_BYTES = 8192
STAGES = frozenset(
    {
        "context",
        "planning",
        "plan-validation",
        "acquisition",
        "evaluator-loading",
        "normalization",
        "evaluation",
        "observation",
        "publication",
    }
)


def _identity(value, pattern):
    return value if type(value) is str and re.fullmatch(pattern, value) is not None else None


@dataclass
class ObserverDiagnostics:
    """Small failure context; no remote text, credentials, paths or exception messages."""

    stage: str = "context"
    arm: str | None = None
    acquisition: acquisition.AcquisitionDiagnostics = field(
        default_factory=acquisition.AcquisitionDiagnostics
    )

    def enter(self, stage, arm=None):
        if stage not in STAGES or (arm is not None and arm not in pilot_tools.ARMS):
            raise ValueError("Invalid observer diagnostic stage")
        self.stage, self.arm = stage, arm

    def failure(self, args, error):
        # Only fixed categories and explicitly allowlisted acquisition codes are
        # publishable. Exception messages can contain tokens or remote payloads.
        if isinstance(error, acquisition.AcquisitionError):
            category, code = "acquisition", acquisition.diagnostic_code(error)
        elif isinstance(error, normalization.NormalizationError):
            category, code = "validation", "normalization-rejected"
        elif isinstance(error, OSError):
            category, code = "io", "io-failed"
        elif isinstance(error, ImportError):
            category, code = "dependency", "evaluator-unavailable"
        elif isinstance(error, (ValueError, TypeError, KeyError, RecursionError)):
            category, code = "validation", "invalid-data"
        else:
            category, code = "runtime", self.stage + "-failed"
        return {
            "test-shadow-diagnostics-version": 1,
            "authority": "diagnostic-only",
            "activation": "disabled",
            "status": "incomplete",
            "stage": self.stage,
            "arm": self.arm,
            "category": category,
            "code": code,
            "group": args.group if args.group in pilot_tools.ARMS else None,
            "run_id": _identity(args.run_id, r"[1-9][0-9]{0,15}"),
            "attempt": args.attempt
            if type(args.attempt) is int and 0 < args.attempt <= 2**31 - 1
            else None,
            "source": {
                name: _identity(getattr(args, name), r"[0-9a-f]{40}")
                for name in ("execution_commit", "execution_tree", "adapter_commit", "adapter_tree")
            },
            "acquisition": self.acquisition.snapshot(),
        }


def retain_failure(args, diagnostics, error):
    """Best-effort bounded diagnostics never turn an observation failure into success."""
    report = diagnostics.failure(args, error)
    raw = shadow.encode(report)
    if len(raw) > MAX_DIAGNOSTIC_BYTES:
        raise ValueError("Observer diagnostics exceed their fixed byte limit")
    envelope = {
        "test-shadow-publication-version": 1,
        "activation": "disabled",
        "status": "incomplete",
        "group": report["group"],
        "run_id": report["run_id"],
        "attempt": report["attempt"],
        "error": "Receipt acquisition, normalization or evaluation did not complete",
    }
    for name, data in (("diagnostics.json", raw), ("error.json", shadow.encode(envelope))):
        try:
            (args.output / name).write_bytes(data)
        except OSError:
            # A full/read-only output filesystem cannot retain its own failure.
            # The fixed console category and nonzero exit must still survive.
            pass
    print(
        "Advisory observation incomplete: "
        f"stage={report['stage']} category={report['category']} code={report['code']}; "
        "inspect diagnostics.json when available"
    )


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
    directory: Path,
    output: Path,
    scratch: Path,
    group: str,
    run_id: str,
    attempt: int,
    *,
    diagnostics: ObserverDiagnostics | None = None,
):
    """Authenticate real receipts, then call YAGA's installed pure evaluator."""
    progress = diagnostics or ObserverDiagnostics()
    progress.enter("plan-validation")
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
    progress.enter("acquisition")
    if diagnostics is None:
        acquired = wait_for_receipts(expected, scratch)
    else:
        acquired = wait_for_receipts(
            expected,
            scratch,
            acquire=lambda context, destination: acquisition.acquire_receipts(
                context, destination, diagnostics=progress.acquisition
            ),
        )
    progress.enter("evaluator-loading")
    plan_decoder = importlib.import_module("yaga.test_planning.diagnostics")
    evaluator = importlib.import_module("yaga.test_planning.evaluation")
    result_parser = importlib.import_module("yaga.test_planning.results")
    exit_code = 0
    for arm in aliases:
        progress.enter("plan-validation", arm)
        arm_expected = replace(
            expected,
            plan_digest=pilot["arms"][arm]["plan_digest"],
            policy_sha256=pilot["arms"][arm]["policy_sha256"],
        )
        plan_document = shadow.decode(shadow.read_bounded(directory / f"pilot-{arm}-plan.json"))
        planned = plan_decoder.parse_plan_document(plan_document)
        progress.enter("normalization", arm)
        document = normalization.normalize_results(
            acquired.root, expected=arm_expected, provenance=acquired.provenance
        )
        results = result_parser.parse_results_document(document)
        progress.enter("evaluation", arm)
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
        progress.enter("observation", arm)
        observation = make_observation(
            pilot,
            arm,
            arm_expected,
            document,
            json.loads(raw_evaluation),
            raw_evaluation,
            acquired.evidence,
        )
        progress.enter("publication", arm)
        (output / f"{arm}-plan.json").write_bytes(shadow.encode(plan_document))
        (output / f"{arm}-results.json").write_bytes(result_parser.encode_results(results))
        (output / f"{arm}-evaluation.json").write_bytes(raw_evaluation)
        (output / f"{arm}-observation.json").write_bytes(experiment.encode_observation(observation))
        exit_code = max(exit_code, evaluated.exit_code)
    progress.enter("publication")
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
    diagnostics = ObserverDiagnostics()
    try:
        if (
            args.attempt != 1
            or args.execution_commit != args.adapter_commit
            or args.execution_tree != args.adapter_tree
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        ):
            raise shadow.ShadowError("Fresh manual same-source shadow run required")
        diagnostics.enter("planning")
        regenerate(args)
        return evaluate_group(
            args.directory,
            args.output,
            args.scratch,
            args.group,
            args.run_id,
            args.attempt,
            diagnostics=diagnostics,
        )
    except (
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        ImportError,
        RecursionError,
    ) as error:
        retain_failure(args, diagnostics, error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
