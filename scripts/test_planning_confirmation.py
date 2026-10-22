"""One bounded observer-only diagnostic over the immutable issue #644 evidence.

This is neither a qualification run nor a replacement shadow publication. Historical
documents remain inert data; only the current checked-out observer code is executed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from scripts import test_planning_acquisition as acquisition
from scripts import test_planning_observer as observer
from scripts import test_planning_results as normalization
from scripts import test_planning_shadow as shadow

RUN_ID = "39141298183"
COMMIT = "5d8e53e1c4c9297a5cae5fe35f52edb089ba7b27"
TREE = "13f4a2d5be3e46e296500d0d5a029aedc364a334"
BASE = "44c8ae23c0bed9a57d0b71d6f0a549b1944cb871"
PLANNING_JOB = 117716115945
PLANNING_ARTIFACT = 12141632930
PLANNING_SHA256 = "8da504f6bcff054884b31e20d9c4c80941ddc593a9d931cbe47e8a39a7d2891b"
POLICY_SHA256 = "86e5c933945edd5fdb2d9434d26ee67c6eb5700914bbf7329799ba5c304ded19"
BINDINGS_SHA256 = "68fa4a07052777c4a8717dbce22a4d789f135d2ca73ece6abe1c3dfa275a2ab2"
PLAN_DIGESTS = {
    "full": "aa9438e51b423c23990c1f992524433e93adee9f0d245a362a3418b00e0843b0",
    "proposed": "82adf4ff4b2e837eb4f8e45906ae2a94bf29ca792287cba58183ed0db3b11c40",
}
PLAN_FILES = {
    "pilot.json": "8094e072691c3125d17f2b048608fb1c3a8df5bf98abd4dfe1ead25b6d7878c7",
    "pilot-full-plan.json": "a4fbdeeda9d631d5f4b56272aa7e00bcf471c65b3a41df056e3d07e05d194e13",
    "pilot-proposed-plan.json": "67e3357d15dbc4b4c88832e1f6921384a454dd42c10bf2b8d52c48124f1e19f2",
}
CELLS = tuple(
    normalization.Cell("qualification." + category, variant, binding)
    for category, binding, variants in (
        ("application", "application-qualification", ("default",)),
        ("cancellation", "cancellation-qualification", ("default",)),
        ("latency", "latency-qualification", ("default",)),
        ("monitoring", "monitoring-qualification", ("default",)),
        ("native", "upgrade-native-qualification", ("core-loss", "jobs", "jobs-crash", "native")),
    )
    for variant in variants
)
WORKFLOW = ".github/workflows/test-planning-observer-confirmation.yml"
UPLOAD_STEP = "Retain bounded comparison and immutable manifest"
MAX_CACHE_BYTES = 32 * acquisition.MIB
MAX_PLANNING_ZIP = acquisition.MIB
SOURCE_FILES = (
    "scripts/test_planning_confirmation.py",
    "scripts/test_planning_observer.py",
    "scripts/test_planning_acquisition.py",
    "scripts/test_planning_results.py",
)
require = acquisition.require


def expected_run(arm="full"):
    return normalization.ExpectedRun(
        PLAN_DIGESTS[arm], POLICY_SHA256, BINDINGS_SHA256, COMMIT, TREE, RUN_ID, 1, CELLS
    )


class RecordingTransport:
    """Share the production request/deadline budget and retain bounded replay bytes."""

    def __init__(self, client):
        self.client = client
        self.responses = []
        self.total = 0
        self.calls = 0

    def get(self, route, maximum):
        self.calls += 1
        require(self.calls <= 100, "acquisition-request-budget")
        raw = self.client.get(route, maximum)
        require(type(raw) is bytes and len(raw) <= maximum, "api-response-budget")
        self.total += len(raw)
        require(self.total <= MAX_CACHE_BYTES, "all-receipt-byte-budget")
        self.responses.append((route, maximum, raw))
        return raw

    def manifest(self):
        return [
            {
                "route": acquisition._diagnostic_route(route)[0],
                "object_id": acquisition._diagnostic_route(route)[1],
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for route, _, raw in self.responses
        ]


def authenticate_plans(client):
    """Verify the fixed planning upload, then parse only three pinned data files."""
    expected = expected_run()
    prefix = acquisition.PREFIX
    route = f"{prefix}/actions/runs/{RUN_ID}"

    def read(endpoint):
        value = shadow.decode(client.get(endpoint, acquisition.MAX_JSON), acquisition.MAX_JSON)
        require(type(value) is dict, "invalid-github-object")
        return value

    run = read(route)
    acquisition.validate_run(run, expected)
    require(run["status"] == "completed", "selected-job-incomplete")
    acquisition.validate_run(read(route + "/attempts/1"), expected)
    job_route = f"{prefix}/actions/jobs/{PLANNING_JOB}"
    job = read(job_route)
    for key, value in {
        "id": PLANNING_JOB,
        "run_id": int(RUN_ID),
        "run_attempt": 1,
        "name": "Shadow Plan Comparison",
        "workflow_name": acquisition.WORKFLOW_NAME,
        "head_sha": COMMIT,
        "head_branch": "main",
        "status": "completed",
        "conclusion": "success",
    }.items():
        acquisition._same(job.get(key), value, "wrong-job-lineage")
    start = acquisition._timestamp(job.get("started_at"))
    end = acquisition._timestamp(job.get("completed_at"))
    require(
        acquisition._timestamp(run["run_started_at"]) <= start <= end, "invalid-job-time-window"
    )
    steps = job.get("steps")
    require(
        type(steps) is list and len(steps) <= 100 and all(type(step) is dict for step in steps),
        "invalid-job-steps",
    )
    uploads = [step for step in steps if step.get("name") == UPLOAD_STEP]
    require(len(uploads) == 1, "missing-upload-step")
    upload = uploads[0]
    require(
        upload.get("status") == "completed" and upload.get("conclusion") == "success",
        "required-job-step-not-successful",
    )
    require(
        start
        <= acquisition._timestamp(upload.get("started_at"))
        <= acquisition._timestamp(upload.get("completed_at"))
        <= end,
        "invalid-job-step-time-window",
    )
    artifact_route = f"{prefix}/actions/artifacts/{PLANNING_ARTIFACT}"
    artifact = read(artifact_route)
    acquisition.validate_artifact_metadata(
        artifact, expected, upload, f"test-planning-shadow-{RUN_ID}-1"
    )
    require(
        artifact["id"] == PLANNING_ARTIFACT
        and artifact["digest"] == "sha256:" + PLANNING_SHA256
        and artifact["size_in_bytes"] <= MAX_PLANNING_ZIP,
        "invalid-artifact-identity",
    )
    acquisition.validate_upload_log(
        client.get(job_route + "/logs", acquisition.MAX_LOG), artifact, upload
    )
    raw = client.get(artifact_route + "/zip", MAX_PLANNING_ZIP)
    require(
        len(raw) == artifact["size_in_bytes"]
        and hashlib.sha256(raw).hexdigest() == PLANNING_SHA256,
        "artifact-archive-digest-mismatch",
    )
    files = acquisition.extract_receipts(raw, frozenset(PLAN_FILES), max_file_bytes=65536)
    acquisition._same(read(job_route), job, "job-changed-during-acquisition")
    acquisition._same(read(artifact_route), artifact, "artifact-changed-during-acquisition")
    after = read(route)
    acquisition.validate_run(after, expected)
    acquisition._same(after, run, "wrong-shadow-run")
    for name, digest in PLAN_FILES.items():
        require(hashlib.sha256(files[name]).hexdigest() == digest, "invalid-plan-identity")
    pilot = shadow.decode(files["pilot.json"])
    source = {
        "base_commit": BASE,
        "head_commit": COMMIT,
        "comparison_commit": BASE,
        "execution_commit": COMMIT,
        "execution_tree": TREE,
    }
    acquisition._same(pilot["source"], source, "wrong-shadow-source")
    acquisition._same(
        pilot["adapter_source"], {"commit": COMMIT, "tree": TREE}, "wrong-shadow-source"
    )
    acquisition._same(pilot["groups"]["full"], ["full", "proposed"], "invalid-plan-identity")
    documents = {}
    for arm in PLAN_DIGESTS:
        require(
            observer.expected_run(pilot, arm, RUN_ID, 1) == expected_run(arm),
            "invalid-plan-identity",
        )
        document = shadow.decode(files[f"pilot-{arm}-plan.json"])
        require(document["plan_digest"] == PLAN_DIGESTS[arm], "invalid-plan-identity")
        acquisition._same(document["plan"]["source"], source, "wrong-shadow-source")
        documents[arm] = document
    return documents


def verify_context(args):
    """Bind diagnostic code and invocation independently from historical evidence."""
    shadow.oid(args.observer_commit)
    shadow.oid(args.observer_tree)
    require(
        re.fullmatch(r"[1-9][0-9]{0,15}", args.run_id) is not None
        and args.run_id != RUN_ID
        and type(args.attempt) is int
        and 0 < args.attempt <= 2**31 - 1,
        "invalid-run-identity",
    )
    for name, wanted in {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": acquisition.REPOSITORY,
        "GITHUB_SHA": args.observer_commit,
        "GITHUB_WORKFLOW_SHA": args.observer_commit,
        "GITHUB_RUN_ID": args.run_id,
        "GITHUB_RUN_ATTEMPT": str(args.attempt),
    }.items():
        require(os.environ.get(name) == wanted, "wrong-shadow-source")
    require(
        os.environ.get("GITHUB_WORKFLOW_REF", "").startswith(
            acquisition.REPOSITORY + "/" + WORKFLOW + "@refs/heads/"
        ),
        "wrong-workflow",
    )
    require(Path(__file__).resolve().parent.parent == args.repo.resolve(), "wrong-shadow-source")
    executable = shutil.which("git")
    require(executable is not None, "wrong-shadow-source")
    executable = str(executable)

    def git(*options):
        result = subprocess.run(
            [executable, "-C", str(args.repo), *options],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        require(result.returncode == 0 and len(result.stdout) <= 4096, "wrong-shadow-source")
        return result.stdout.decode("ascii").strip()

    require(git("rev-parse", "HEAD") == args.observer_commit, "wrong-shadow-source")
    require(git("rev-parse", "HEAD^{tree}") == args.observer_tree, "wrong-shadow-source")
    git("diff", "--quiet", "--no-ext-diff", "HEAD", "--")
    require(importlib.metadata.version("yaga-cli") == "0.3.0", "invalid-plan-identity")
    return {
        "commit": args.observer_commit,
        "tree": args.observer_tree,
        "files": {
            name: hashlib.sha256(shadow.read_bounded(args.repo / name)).hexdigest()
            for name in SOURCE_FILES
        },
        "evaluator": {"distribution": "yaga-cli", "version": "0.3.0"},
    }


def diagnostic_args(output, *, observer_commit, observer_tree):
    return SimpleNamespace(
        output=output,
        group="full",
        run_id=RUN_ID,
        attempt=1,
        execution_commit=COMMIT,
        execution_tree=TREE,
        adapter_commit=observer_commit,
        adapter_tree=observer_tree,
    )


def timeout_canary(responses, destination, output, *, observer_commit, observer_tree):
    """Replay cached acquisition bytes and fail at its first log; never use a client."""

    class CachedTimeout:
        def __init__(self):
            self.index = 0

        def get(self, route, maximum):
            require(self.index < len(responses), "acquisition-request-budget")
            expected_route, expected_maximum, raw = responses[self.index]
            require(
                (route, maximum) == (expected_route, expected_maximum), "unsupported-github-route"
            )
            self.index += 1
            if route.endswith("/logs"):
                raise acquisition.AcquisitionError("github-request-timeout")
            return raw

    output.mkdir()
    progress = observer.ObserverDiagnostics()
    progress.enter("acquisition")
    try:
        acquisition.acquire_receipts(
            expected_run(), destination, github=CachedTimeout(), diagnostics=progress.acquisition
        )
    except acquisition.AcquisitionError as error:
        observer.retain_failure(
            diagnostic_args(output, observer_commit=observer_commit, observer_tree=observer_tree),
            progress,
            error,
        )
        code = 1
    else:
        code = 0
    require(code == 1, "invalid-acquisition-data")
    raw = shadow.read_bounded(output / "diagnostics.json", observer.MAX_DIAGNOSTIC_BYTES)
    diagnostic = shadow.decode(raw)
    require(
        diagnostic["code"] == "github-request-timeout"
        and diagnostic["stage"] == "acquisition"
        and diagnostic["status"] == "incomplete"
        and diagnostic["authority"] == "diagnostic-only"
        and not (output / "publication.json").exists(),
        "invalid-acquisition-data",
    )
    return {
        "exit_code": code,
        "code": diagnostic["code"],
        "diagnostics_sha256": hashlib.sha256(raw).hexdigest(),
        "live_requests": 0,
    }


def confirm(args, *, github=None):
    """Execute one acquisition and two alias evaluations, never a workload or poll."""
    client = None
    progress = observer.ObserverDiagnostics()
    report = {
        "observer-confirmation-version": 1,
        "authority": "diagnostic-only",
        "activation": "disabled",
        "status": "incomplete",
        "observer_source": None,
        "diagnostic_run": {
            "run_id": observer._identity(args.run_id, r"[1-9][0-9]{0,15}"),
            "attempt": args.attempt
            if type(args.attempt) is int and 0 < args.attempt <= 2**31 - 1
            else None,
        },
        "historical_evidence": {
            "run_id": RUN_ID,
            "attempt": 1,
            "commit": COMMIT,
            "tree": TREE,
            "base": BASE,
            "planning_artifact_id": PLANNING_ARTIFACT,
            "planning_sha256": PLANNING_SHA256,
            "policy_sha256": POLICY_SHA256,
            "bindings_sha256": BINDINGS_SHA256,
            "plan_digests": PLAN_DIGESTS,
        },
        "qualification_workloads": 0,
        "polls": 0,
        "retries": 0,
    }
    try:
        report["observer_source"] = verify_context(args)
        client = RecordingTransport(github if github is not None else acquisition.Github())
        progress.enter("planning")
        documents = authenticate_plans(client)
        start = len(client.responses)
        progress.enter("acquisition")
        acquired = acquisition.acquire_receipts(
            expected_run(),
            args.scratch / "receipt-acquisition",
            github=client,
            diagnostics=progress.acquisition,
        )
        receipt_responses = client.responses[start:]
        report["evaluations"] = {}
        for arm in PLAN_DIGESTS:
            progress.enter("plan-validation", arm)
            document, raw_results, evaluated, raw_evaluation = observer.evaluate_acquired(
                documents[arm], expected_run(arm), acquired, diagnostics=progress
            )
            require(
                evaluated.exit_code == 0
                and len(document["results"]) == 8
                and all(item["outcome"] == "passed" for item in document["results"]),
                "invalid-acquisition-data",
            )
            (args.output / f"historical-{arm}-results.json").write_bytes(raw_results)
            (args.output / f"historical-{arm}-evaluation.json").write_bytes(raw_evaluation)
            report["evaluations"][arm] = {
                "status": "passed",
                "cells": 8,
                "results_sha256": hashlib.sha256(raw_results).hexdigest(),
                "evaluation_sha256": hashlib.sha256(raw_evaluation).hexdigest(),
            }
        progress.enter("acquisition")
        report["timeout_canary"] = timeout_canary(
            receipt_responses,
            args.scratch / "timeout-acquisition",
            args.output / "timeout-canary",
            observer_commit=args.observer_commit,
            observer_tree=args.observer_tree,
        )
        report["status"] = "passed"
    except (
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        ImportError,
        RecursionError,
        subprocess.SubprocessError,
    ) as error:
        failure = args.output / "failure"
        failure.mkdir()
        observer.retain_failure(
            diagnostic_args(
                failure, observer_commit=args.observer_commit, observer_tree=args.observer_tree
            ),
            progress,
            error,
        )
        report["status"] = "incomplete"
    report["responses"] = client.manifest() if client is not None else []
    report["request_count"] = client.calls if client is not None else 0
    report["completed_response_count"] = len(client.responses) if client is not None else 0
    report["response_bytes"] = client.total if client is not None else 0
    (args.output / "confirmation.json").write_bytes(shadow.encode(report))
    return 0 if report["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "output", "scratch"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("observer-commit", "observer-tree", "run-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--attempt", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        output, scratch = args.output.absolute(), args.scratch.absolute()
        for path in (output, scratch):
            acquisition._no_links(path)
            require(not path.exists() and path.parent.is_dir(), "output-directory-not-fresh")
        require(
            output != scratch and output not in scratch.parents and scratch not in output.parents,
            "output-directory-not-fresh",
        )
        output.mkdir()
        scratch.mkdir()
        return confirm(args)
    except (
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        ImportError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        print("Observer confirmation incomplete; no new qualification evidence was produced")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
