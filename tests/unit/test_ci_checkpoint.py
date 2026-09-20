"""Qualification follows current CI attempts without hiding real failures."""

import pytest

from scripts.wait_for_ci_checkpoint import CheckpointError, inspect_checkpoint, wait_for_checkpoint

SHA = "a" * 40
REPO = "owner/repo"


def run(number=2, **changes):
    return {
        "id": number,
        "run_number": number,
        "run_attempt": 1,
        "head_sha": SHA,
        "path": ".github/workflows/ci.yml",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        **changes,
    }


def api_for(runs, *, gate="success", confirmed=None):
    reads = []

    def api(endpoint):
        reads.append(endpoint)
        if "/jobs?" in endpoint:
            return {"total_count": 1, "jobs": [{"name": "CI Gate", "conclusion": gate}]}
        return {
            "total_count": len(runs),
            "workflow_runs": confirmed if confirmed is not None and len(reads) > 1 else runs,
        }

    return api, reads


def test_old_failed_gate_cannot_override_new_workflow_before_aggregate_exists():
    api, reads = api_for([run(1, conclusion="failure"), run(status="in_progress", conclusion=None)])
    assert inspect_checkpoint(api, REPO, SHA) is None
    assert len(reads) == 1


def test_current_attempt_is_checked_instead_of_earlier_green_attempt():
    api, reads = api_for([run(run_attempt=3)])
    result = inspect_checkpoint(api, REPO, SHA)
    assert result is not None
    assert result.run_id == 2 and result.attempt == 3
    assert "/attempts/3/jobs?" in reads[1]


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "skipped", "neutral"])
def test_real_current_failure_cannot_fall_back_to_historical_success(conclusion):
    api, _ = api_for([run(1), run(conclusion=conclusion)])
    with pytest.raises(CheckpointError, match="did not pass"):
        inspect_checkpoint(api, REPO, SHA)


@pytest.mark.parametrize(
    "change",
    [
        {"head_sha": "b" * 40},
        {"path": ".github/workflows/other.yml"},
        {"event": "pull_request_target"},
    ],
)
def test_unrelated_or_untrusted_workflow_cannot_satisfy_checkpoint(change):
    api, _ = api_for([run(**change)])
    assert inspect_checkpoint(api, REPO, SHA) is None


def test_replacement_appearing_during_inspection_must_be_waited_for():
    api, _ = api_for([run()], confirmed=[run(3, status="queued", conclusion=None)])
    assert inspect_checkpoint(api, REPO, SHA) is None


@pytest.mark.parametrize(
    "runs", [[], [run(conclusion="cancelled")], [run(status="queued", conclusion=None)]]
)
def test_missing_cancelled_and_pending_runs_have_a_finite_deadline(runs):
    api, _ = api_for(runs)
    now = [0]

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(CheckpointError, match="deadline"):
        wait_for_checkpoint(
            api, REPO, SHA, timeout=7, interval=5, clock=lambda: now[0], sleep=sleep
        )
    assert now[0] == 7


def test_workflow_success_without_passing_gate_is_rejected():
    api, _ = api_for([run()], gate="skipped")
    with pytest.raises(CheckpointError, match="unique passing"):
        inspect_checkpoint(api, REPO, SHA)


def test_success_arriving_after_deadline_is_not_accepted():
    now = [0]
    api, _ = api_for([run()])

    def slow_api(endpoint):
        now[0] += 4
        return api(endpoint)

    with pytest.raises(CheckpointError, match="deadline"):
        wait_for_checkpoint(
            slow_api, REPO, SHA, timeout=7, clock=lambda: now[0], sleep=lambda _: None
        )


@pytest.mark.parametrize("gates", [[], [{"name": "CI Gate", "conclusion": "success"}] * 2])
def test_missing_or_duplicate_gate_cannot_prove_acceptance(gates):
    api, _ = api_for([run()])

    def malformed(endpoint):
        return {"total_count": len(gates), "jobs": gates} if "/jobs?" in endpoint else api(endpoint)

    with pytest.raises(CheckpointError, match="unique passing"):
        inspect_checkpoint(malformed, REPO, SHA)


def test_truncated_run_inventory_is_rejected():
    with pytest.raises(CheckpointError, match="inventory"):
        inspect_checkpoint(lambda _: {"total_count": 101, "workflow_runs": [run()]}, REPO, SHA)
