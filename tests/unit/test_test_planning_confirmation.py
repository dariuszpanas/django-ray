"""The diagnostic reuses inert evidence; no test contacts GitHub or starts workloads."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import test_planning_acquisition as acquisition
from scripts import test_planning_confirmation as confirmation
from scripts import test_planning_observer as observer
from scripts import test_planning_shadow as shadow
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


@pytest.fixture
def github(monkeypatch):
    for name, value in (("RUN_ID", RUN), ("COMMIT", SOURCE), ("TREE", TREE)):
        monkeypatch.setattr(confirmation, name, value)
    client = GithubFixture(confirmation.CELLS)
    client.run["status"] = "completed"
    source = {
        "base_commit": confirmation.BASE,
        "comparison_commit": confirmation.BASE,
        "head_commit": SOURCE,
        "execution_commit": SOURCE,
        "execution_tree": TREE,
    }
    pilot = {
        "source": source,
        "adapter_source": {"commit": SOURCE, "tree": TREE},
        "bindings_sha256": confirmation.BINDINGS_SHA256,
        "groups": {"full": ["full", "proposed"]},
        "arms": {
            arm: {
                "plan_digest": digest,
                "policy_sha256": confirmation.POLICY_SHA256,
                "selected": [asdict(cell) for cell in confirmation.CELLS],
            }
            for arm, digest in confirmation.PLAN_DIGESTS.items()
        },
    }
    files = {
        "pilot.json": shadow.encode(pilot),
        **{
            f"pilot-{arm}-plan.json": shadow.encode(
                {"plan_digest": digest, "plan": {"source": source}}
            )
            for arm, digest in confirmation.PLAN_DIGESTS.items()
        },
    }
    monkeypatch.setattr(
        confirmation,
        "PLAN_FILES",
        {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    )
    raw = zip_bytes(files.items())
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(confirmation, "PLANNING_SHA256", digest)
    job = {
        "id": confirmation.PLANNING_JOB,
        "run_id": int(RUN),
        "run_attempt": 1,
        "name": "Shadow Plan Comparison",
        "workflow_name": acquisition.WORKFLOW_NAME,
        "head_sha": SOURCE,
        "head_branch": "main",
        "status": "completed",
        "conclusion": "success",
        "started_at": START,
        "completed_at": END,
        "steps": [
            {
                "name": confirmation.UPLOAD_STEP,
                "status": "completed",
                "conclusion": "success",
                "started_at": UPLOAD_START,
                "completed_at": END,
            }
        ],
    }
    artifact = copy.deepcopy(client.artifacts[0])
    artifact.update(
        id=confirmation.PLANNING_ARTIFACT,
        name=f"test-planning-shadow-{RUN}-1",
        digest="sha256:" + digest,
        size_in_bytes=len(raw),
    )
    prefix = acquisition.PREFIX
    client.replies[f"{prefix}/actions/jobs/{confirmation.PLANNING_JOB}"] = job
    client.replies[f"{prefix}/actions/jobs/{confirmation.PLANNING_JOB}/logs"] = (
        f"{UPLOADED} SHA256 digest of uploaded artifact is {digest}\n"
        f"{UPLOADED} Artifact {artifact['name']} has been successfully uploaded! Final size is {len(raw)} bytes. Artifact ID is {artifact['id']}\n"
    ).encode()
    client.replies[f"{prefix}/actions/artifacts/{confirmation.PLANNING_ARTIFACT}"] = artifact
    client.replies[f"{prefix}/actions/artifacts/{confirmation.PLANNING_ARTIFACT}/zip"] = raw
    return client


def args_for(tmp_path):
    return SimpleNamespace(
        repo=Path(confirmation.__file__).resolve().parent.parent,
        output=tmp_path / "output",
        scratch=tmp_path / "scratch",
        observer_commit="c" * 40,
        observer_tree="d" * 40,
        run_id="9876",
        attempt=1,
    )


def test_planning_bundle_is_bound_to_fixed_upload_and_never_executed(github):
    documents = confirmation.authenticate_plans(confirmation.RecordingTransport(github))
    assert set(documents) == {"full", "proposed"}
    assert len(github.calls) == 9
    assert all(acquisition._endpoint(route) for route in github.calls)


@pytest.mark.parametrize(
    "fault", ["expired", "identity", "digest", "upload", "tamper", "source", "changed", "pending"]
)
def test_planning_authentication_fails_closed(github, fault):
    prefix = acquisition.PREFIX
    artifact_route = f"{prefix}/actions/artifacts/{confirmation.PLANNING_ARTIFACT}"
    job_route = f"{prefix}/actions/jobs/{confirmation.PLANNING_JOB}"
    if fault == "expired":
        github.replies[artifact_route]["expired"] = True
    elif fault == "identity":
        github.replies[artifact_route]["id"] += 1
    elif fault == "digest":
        github.replies[artifact_route]["digest"] = "sha256:" + "0" * 64
    elif fault == "upload":
        github.replies[job_route + "/logs"] = b"no verified upload\n"
    elif fault == "tamper":
        github.replies[artifact_route + "/zip"] += b"tamper"
    elif fault == "source":
        github.run["head_commit"]["tree_id"] = "0" * 40
    elif fault == "pending":
        github.run["status"] = "in_progress"
    else:
        original = github.replies[job_route]
        github.replies[job_route] = lambda: (
            original if github.calls.count(job_route) == 1 else {**original, "id": 9}
        )
    with pytest.raises(acquisition.AcquisitionError):
        confirmation.authenticate_plans(confirmation.RecordingTransport(github))
    assert len(github.calls) <= 10


def test_pinned_plan_bytes_are_checked_independently(github, monkeypatch):
    monkeypatch.setattr(
        confirmation, "PLAN_FILES", {**confirmation.PLAN_FILES, "pilot.json": "0" * 64}
    )
    with pytest.raises(acquisition.AcquisitionError, match="invalid-plan-identity"):
        confirmation.authenticate_plans(confirmation.RecordingTransport(github))


def test_timeout_canary_uses_only_recorded_bytes_and_production_failure_path(
    github, tmp_path, monkeypatch
):
    recorded = confirmation.RecordingTransport(github)
    acquisition.acquire_receipts(
        confirmation.expected_run(), tmp_path / "original", github=recorded
    )
    count = len(github.calls)
    monkeypatch.setattr(acquisition, "Github", lambda: pytest.fail("network client constructed"))
    result = confirmation.timeout_canary(
        recorded.responses,
        tmp_path / "canary-acquisition",
        tmp_path / "canary",
        observer_commit="c" * 40,
        observer_tree="d" * 40,
    )
    assert result["exit_code"] == 1 and result["code"] == "github-request-timeout"
    assert len(github.calls) == count
    assert not (tmp_path / "canary/publication.json").exists()
    diagnostic = json.loads((tmp_path / "canary/diagnostics.json").read_bytes())
    assert diagnostic["stage"] == "acquisition"
    assert diagnostic["acquisition"]["last_route_kind"] == "job-log"
    assert diagnostic["source"]["adapter_commit"] == "c" * 40
    assert diagnostic["source"]["execution_commit"] == SOURCE
    assert (tmp_path / "canary/diagnostics.json").stat().st_size <= observer.MAX_DIAGNOSTIC_BYTES


def test_confirmation_acquires_once_and_evaluates_both_aliases_without_publication(
    github, tmp_path, monkeypatch
):
    args = args_for(tmp_path)
    args.output.mkdir()
    args.scratch.mkdir()
    monkeypatch.setattr(
        confirmation,
        "verify_context",
        lambda _: {"commit": args.observer_commit, "tree": args.observer_tree},
    )
    monkeypatch.setattr(
        observer, "wait_for_receipts", lambda *_a, **_kw: pytest.fail("polling forbidden")
    )
    calls = []

    def evaluate(document, expected, acquired, *, diagnostics):
        calls.append((expected, acquired))
        value = {"results": [{"outcome": "passed"}] * 8}
        return value, shadow.encode(value), SimpleNamespace(exit_code=0), b'{"status":"passed"}'

    monkeypatch.setattr(observer, "evaluate_acquired", evaluate)
    assert confirmation.confirm(args, github=github) == 0
    report = json.loads((args.output / "confirmation.json").read_bytes())
    assert report["status"] == "passed" and report["authority"] == "diagnostic-only"
    assert len(calls) == 2 and calls[0][1] is calls[1][1]
    assert report["request_count"] == 57
    assert report["observer_source"]["commit"] != report["historical_evidence"]["commit"]
    assert not list(args.output.rglob("publication.json"))
    assert not list(args.output.rglob("*.log")) and not list(args.output.rglob("*.zip"))
    assert report["timeout_canary"]["live_requests"] == 0


def test_pending_receipts_fail_after_one_observation_without_retry(github, tmp_path, monkeypatch):
    args = args_for(tmp_path)
    args.output.mkdir()
    args.scratch.mkdir()
    monkeypatch.setattr(confirmation, "verify_context", lambda _: {})
    github.jobs[0]["status"] = "in_progress"
    assert confirmation.confirm(args, github=github) == 1
    assert github.calls.count(github.jobs_route) == 1
    report = json.loads((args.output / "confirmation.json").read_bytes())
    assert report["status"] == "incomplete"
    assert "timeout_canary" not in report
    assert (
        json.loads((args.output / "failure/diagnostics.json").read_bytes())["code"]
        == "selected-job-incomplete"
    )


def test_recording_transport_retains_no_over_budget_response(github, monkeypatch):
    monkeypatch.setattr(confirmation, "MAX_CACHE_BYTES", 1)
    client = confirmation.RecordingTransport(github)
    with pytest.raises(acquisition.AcquisitionError, match="all-receipt-byte-budget"):
        client.get(acquisition.PREFIX, acquisition.MAX_JSON)
    assert client.responses == [] and client.calls == 1


def test_failed_transport_request_is_counted_without_publishing_exception_text():
    class Failed:
        def get(self, *_args):
            raise acquisition.AcquisitionError("github-request-timeout")

    client = confirmation.RecordingTransport(Failed())
    with pytest.raises(acquisition.AcquisitionError, match="github-request-timeout"):
        client.get(acquisition.PREFIX, acquisition.MAX_JSON)
    assert client.calls == 1 and client.responses == []


def test_context_failure_retains_incomplete_without_claiming_verified_source(tmp_path, monkeypatch):
    args = args_for(tmp_path)
    args.output.mkdir()
    args.scratch.mkdir()

    def fail(_args):
        raise ValueError("PRIVATE_EXCEPTION_SENTINEL")

    monkeypatch.setattr(confirmation, "verify_context", fail)
    monkeypatch.setattr(
        acquisition, "Github", lambda: pytest.fail("network before context validation")
    )
    assert confirmation.confirm(args) == 1
    raw = (args.output / "confirmation.json").read_bytes()
    report = json.loads(raw)
    assert report["status"] == "incomplete" and report["observer_source"] is None
    assert report["request_count"] == 0
    diagnostic = (args.output / "failure/diagnostics.json").read_bytes()
    assert b"PRIVATE_EXCEPTION_SENTINEL" not in raw + diagnostic
    assert json.loads(diagnostic)["stage"] == "context"


@pytest.mark.parametrize(
    "name,value",
    [("GITHUB_SHA", "0" * 40), ("GITHUB_RUN_ID", RUN), ("GITHUB_EVENT_NAME", "pull_request")],
)
def test_current_source_and_run_context_cannot_impersonate_historical_evidence(
    tmp_path, monkeypatch, name, value
):
    args = args_for(tmp_path)
    for key, wanted in {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": acquisition.REPOSITORY,
        "GITHUB_SHA": args.observer_commit,
        "GITHUB_WORKFLOW_SHA": args.observer_commit,
        "GITHUB_RUN_ID": args.run_id,
        "GITHUB_RUN_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(key, wanted)
    monkeypatch.setenv(name, value)
    with pytest.raises(acquisition.AcquisitionError, match="wrong-shadow-source"):
        confirmation.verify_context(args)


@pytest.mark.parametrize("fault", [None, "commit", "tree", "dirty", "evaluator"])
def test_current_checkout_identity_and_evaluator_are_independently_verified(
    tmp_path, monkeypatch, fault
):
    args = args_for(tmp_path)
    for key, wanted in {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": acquisition.REPOSITORY,
        "GITHUB_SHA": args.observer_commit,
        "GITHUB_WORKFLOW_SHA": args.observer_commit,
        "GITHUB_RUN_ID": args.run_id,
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": acquisition.REPOSITORY
        + "/"
        + confirmation.WORKFLOW
        + "@refs/heads/main",
    }.items():
        monkeypatch.setenv(key, wanted)
    monkeypatch.setattr(confirmation.shutil, "which", lambda _: "git")
    monkeypatch.setattr(
        confirmation.importlib.metadata,
        "version",
        lambda _: "0.2.0" if fault == "evaluator" else "0.3.0",
    )
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == 10
        if command[3:] == ["rev-parse", "HEAD"]:
            raw = "0" * 40 if fault == "commit" else args.observer_commit
        elif command[3:] == ["rev-parse", "HEAD^{tree}"]:
            raw = "0" * 40 if fault == "tree" else args.observer_tree
        else:
            assert command[3:] == ["diff", "--quiet", "--no-ext-diff", "HEAD", "--"]
            return SimpleNamespace(returncode=1 if fault == "dirty" else 0, stdout=b"")
        return SimpleNamespace(returncode=0, stdout=(raw + "\n").encode())

    monkeypatch.setattr(confirmation.subprocess, "run", run)
    if fault is not None:
        with pytest.raises(acquisition.AcquisitionError):
            confirmation.verify_context(args)
    else:
        identity = confirmation.verify_context(args)
        assert identity["commit"] == args.observer_commit
        assert identity["tree"] == args.observer_tree
        assert identity["evaluator"]["version"] == "0.3.0"
        assert set(identity["files"]) == set(confirmation.SOURCE_FILES)
        assert len(commands) == 3


@pytest.mark.parametrize("kind", ["existing", "same", "nested"])
def test_outputs_must_be_new_and_separate(tmp_path, monkeypatch, kind):
    output, scratch = tmp_path / "output", tmp_path / "scratch"
    if kind == "existing":
        output.mkdir()
        (output / "preserve.txt").write_text("existing")
    elif kind == "same":
        scratch = output
    else:
        scratch = output / "scratch"
    monkeypatch.setattr(confirmation, "confirm", lambda _: pytest.fail("invalid output accepted"))
    assert (
        confirmation.main(
            [
                "--repo",
                ".",
                "--output",
                str(output),
                "--scratch",
                str(scratch),
                "--observer-commit",
                "c" * 40,
                "--observer-tree",
                "d" * 40,
                "--run-id",
                "9876",
                "--attempt",
                "1",
            ]
        )
        == 1
    )
    if kind == "existing":
        assert (output / "preserve.txt").read_text() == "existing"
