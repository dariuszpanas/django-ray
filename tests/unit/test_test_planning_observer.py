"""The observer authenticates once, evaluates exact arms, and invents no timing."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from scripts import test_planning_acquisition as acquisition
from scripts import test_planning_observer as observer
from scripts import test_planning_results as normalization
from scripts import test_planning_shadow as shadow

CELL = normalization.Cell("qualification.application", "default", "application-qualification")
COMMIT = "a" * 40
TREE = "b" * 40
RUN_ID = "12345"


def pilot_document(*, empty=False):
    selected = [] if empty else [asdict(CELL)]
    return {
        "source": {
            "base_commit": "c" * 40,
            "head_commit": COMMIT,
            "comparison_commit": "c" * 40,
            "execution_commit": COMMIT,
            "execution_tree": TREE,
        },
        "adapter_source": {"commit": COMMIT, "tree": TREE},
        "bindings_sha256": "c" * 64,
        "groups": {"proposed": ["proposed", "current"]},
        "arms": {
            arm: {
                "plan_digest": str(index + 1) * 64,
                "policy_sha256": "d" * 64 if arm == "current" else "e" * 64,
                "selected": copy.deepcopy(selected),
            }
            for index, arm in enumerate(("current", "proposed", "full"))
        },
    }


def acquired_observation(root, expected, *, outcome="failed", seconds=12.5):
    jobs = tuple(
        normalization.VerifiedJob(cell, index + 1, 1, outcome, ())
        for index, cell in enumerate(expected.selected)
    )
    provenance = normalization.VerifiedRun(
        COMMIT, TREE, RUN_ID, 1, normalization.CONTRACT_PROFILE, jobs, ()
    )
    evidence = {
        "workflow_created_at": "2026-10-18T00:00:00Z",
        "selected_jobs": [
            {
                "cell": asdict(job.cell),
                "job_id": job.job_id,
                "actual_attempt": 1,
                "outcome": outcome,
                "completed_at": "2026-10-18T00:00:30Z",
                "duration_seconds": seconds,
            }
            for job in jobs
        ],
        "artifacts": [],
    }
    return acquisition.Acquisition(root, provenance, evidence)


class FakeClock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_pending_only_polling_preserves_one_expected_context_and_stops_on_receipts(tmp_path):
    clock = FakeClock()
    expected, receipt = object(), object()
    calls = []

    def acquire(context, destination):
        calls.append((context, destination))
        if len(calls) < 3:
            raise acquisition.AcquisitionPending("not-ready")
        return receipt

    assert (
        observer.wait_for_receipts(
            expected, tmp_path, acquire=acquire, clock=clock, sleep=clock.sleep
        )
        is receipt
    )
    assert calls == [(expected, tmp_path)] * 3
    assert clock.sleeps == [60, 60]


@pytest.mark.parametrize("error", [acquisition.AcquisitionError("bad-lineage"), OSError("offline")])
def test_terminal_errors_are_not_polled_or_retried(tmp_path, error):
    clock = FakeClock()
    calls = []

    def acquire(*args):
        calls.append(args)
        raise error

    with pytest.raises(type(error)) as caught:
        observer.wait_for_receipts(None, tmp_path, acquire=acquire, clock=clock, sleep=clock.sleep)
    assert caught.value is error
    assert len(calls) == 1 and clock.sleeps == []


def test_poll_count_budget_does_not_sleep_after_the_last_request(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "MAX_POLLS", 3)
    clock = FakeClock()
    calls = []

    def pending(*args):
        calls.append(args)
        raise acquisition.AcquisitionPending("not-ready")

    with pytest.raises(acquisition.AcquisitionError, match="deadline"):
        observer.wait_for_receipts(None, tmp_path, acquire=pending, clock=clock, sleep=clock.sleep)
    assert len(calls) == 3 and clock.sleeps == [60, 60]


def test_wait_budget_caps_sleep_and_does_not_issue_a_request_after_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "MAX_WAIT_SECONDS", 70)
    clock = FakeClock()
    calls = []

    def pending(*args):
        calls.append(args)
        clock.now += 8
        raise acquisition.AcquisitionPending("not-ready")

    with pytest.raises(acquisition.AcquisitionError, match="deadline"):
        observer.wait_for_receipts(None, tmp_path, acquire=pending, clock=clock, sleep=clock.sleep)
    assert len(calls) == 2 and clock.sleeps == [60]


def test_wait_budget_rejects_a_successful_acquisition_that_arrives_too_late(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "MAX_WAIT_SECONDS", 10)
    clock = FakeClock()

    def late(*_args):
        clock.now = 10
        return object()

    with pytest.raises(acquisition.AcquisitionError, match="deadline"):
        observer.wait_for_receipts(None, tmp_path, acquire=late, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == []


def test_pending_sleep_is_shortened_to_the_remaining_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "MAX_WAIT_SECONDS", 10)
    clock = FakeClock()

    def pending(*_args):
        clock.now += 3
        raise acquisition.AcquisitionPending("not-ready")

    with pytest.raises(acquisition.AcquisitionError, match="deadline"):
        observer.wait_for_receipts(None, tmp_path, acquire=pending, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == [7]


@pytest.mark.parametrize("outcome", ["passed", "failed", "cancelled", "blocked", "pending"])
def test_observation_retains_outcomes_and_unknowns_without_fabricating_feedback(tmp_path, outcome):
    pilot = pilot_document()
    expected = observer.expected_run(pilot, "proposed", RUN_ID, 1)
    acquired = acquired_observation(tmp_path, expected, outcome=outcome, seconds=None)
    results = {"results": [{**asdict(CELL), "outcome": outcome, "evidence_sha256": "f" * 64}]}
    raw_evaluation = b'{"results_sha256":"' + b"e" * 64 + b'"}\n'
    observation = observer.make_observation(
        pilot,
        "proposed",
        expected,
        results,
        json.loads(raw_evaluation),
        raw_evaluation,
        acquired.evidence,
    )
    assert observation["cells"][0]["outcome"] == outcome
    assert observation["cells"][0]["attempts"][0]["runner_seconds"] is None
    assert observation["metrics"]["runner_job_seconds"] is None
    assert (
        observation["identities"]["evaluation_sha256"] == hashlib.sha256(raw_evaluation).hexdigest()
    )
    assert observation["identities"]["source"] == pilot["source"]
    assert observation["timing"] == {
        "workflow_created_at": "2026-10-18T00:00:00Z",
        "first_failure_at": "2026-10-18T00:00:30Z" if outcome == "failed" else None,
        "dispatched_at": None,
        "candidate_ready_at": None,
        "feedback_at": None,
        "required_ci_ready_at": None,
    }
    assert set(observation["fingerprints"].values()) == {None}
    assert observation["cache_state"] == "unknown"
    assert observation["comparison"] is None
    assert bool(observation["incomplete_reason"]) == (
        outcome in {"cancelled", "blocked", "pending"}
    )


@pytest.mark.parametrize("field,value", [("outcome", "passed"), ("actual_attempt", 2)])
def test_acquisition_metadata_cannot_disagree_with_normalized_receipts(tmp_path, field, value):
    pilot = pilot_document()
    expected = observer.expected_run(pilot, "proposed", RUN_ID, 1)
    acquired = acquired_observation(tmp_path, expected)
    acquired.evidence["selected_jobs"][0][field] = value
    results = {"results": [{**asdict(CELL), "outcome": "failed", "evidence_sha256": "f" * 64}]}
    with pytest.raises(shadow.ShadowError, match="differs"):
        observer.make_observation(pilot, "proposed", expected, results, {}, b"", acquired.evidence)


def test_first_terminal_failure_is_ordered_by_time_not_timestamp_text(tmp_path):
    pilot = pilot_document()
    other = normalization.Cell("qualification.monitoring", "default", "monitoring-qualification")
    pilot["arms"]["proposed"]["selected"].append(asdict(other))
    expected = observer.expected_run(pilot, "proposed", RUN_ID, 1)
    acquired = acquired_observation(tmp_path, expected)
    acquired.evidence["selected_jobs"][0]["completed_at"] = "2026-10-18T00:00:30.5Z"
    results = normalization.normalize_results(
        tmp_path, expected=expected, provenance=acquired.provenance
    )
    report = observer.make_observation(
        pilot,
        "proposed",
        expected,
        results,
        {"results_sha256": "e" * 64},
        b"{}\n",
        acquired.evidence,
    )
    assert report["timing"]["first_failure_at"] == "2026-10-18T00:00:30Z"


def fake_yaga(monkeypatch, *, reject=False):
    """Inspect installed API wiring without importing YAGA into ordinary CI."""
    calls = []

    def evaluate(planned, results, **expected):
        calls.append((planned, results, expected))
        if reject:
            raise ValueError("Untrusted evaluation details must not be logged")
        return SimpleNamespace(exit_code=1 if results["results"] else 0, results=results)

    modules = {
        "yaga.test_planning.diagnostics": SimpleNamespace(parse_plan_document=lambda value: value),
        "yaga.test_planning.evaluation": SimpleNamespace(
            evaluate_results=evaluate,
            encode_evaluation=lambda value: shadow.encode(
                {
                    "results_sha256": hashlib.sha256(
                        normalization.canonical(value.results)
                    ).hexdigest()
                }
            ),
        ),
        "yaga.test_planning.results": SimpleNamespace(
            parse_results_document=lambda value: value, encode_results=shadow.encode
        ),
    }
    monkeypatch.setattr(observer.importlib, "import_module", modules.__getitem__)
    return calls


def prepare_group(tmp_path, pilot):
    directory, output, scratch = (tmp_path / name for name in ("plans", "output", "receipts"))
    directory.mkdir()
    output.mkdir()
    (directory / "pilot.json").write_bytes(shadow.encode(pilot))
    for arm in pilot["arms"]:
        (directory / f"pilot-{arm}-plan.json").write_bytes(shadow.encode({"arm": arm}))
    return directory, output, scratch


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "blocked"])
def test_aliases_acquire_once_and_evaluate_their_own_plan_and_policy(
    tmp_path, monkeypatch, outcome
):
    pilot = pilot_document()
    directory, output, scratch = prepare_group(tmp_path, pilot)
    expected = observer.expected_run(pilot, "proposed", RUN_ID, 1)
    acquired = acquired_observation(scratch, expected, outcome=outcome)
    acquisitions = []

    def acquire(context, destination):
        acquisitions.append((context, destination))
        return acquired

    monkeypatch.setattr(observer, "wait_for_receipts", acquire)
    calls = fake_yaga(monkeypatch)
    assert observer.evaluate_group(directory, output, scratch, "proposed", RUN_ID, 1) == 1
    assert acquisitions == [(expected, scratch)]
    assert len(calls) == 2
    for arm, (planned, results, supplied) in zip(("proposed", "current"), calls, strict=True):
        arm_expected = observer.expected_run(pilot, arm, RUN_ID, 1)
        assert planned == {"arm": arm}
        assert results["plan_digest"] == arm_expected.plan_digest
        assert results["policy_sha256"] == arm_expected.policy_sha256
        assert supplied == {
            "expected_policy_sha256": arm_expected.policy_sha256,
            "expected_bindings_sha256": arm_expected.bindings_sha256,
            "expected_execution_commit": COMMIT,
            "expected_execution_tree": TREE,
            "expected_run_id": RUN_ID,
            "expected_attempt": 1,
        }
        assert (
            json.loads((output / f"{arm}-results.json").read_bytes())["results"][0]["outcome"]
            == outcome
        )
        assert (
            json.loads((output / f"{arm}-observation.json").read_bytes())["timing"]["feedback_at"]
            is None
        )
    publication = json.loads((output / "publication.json").read_bytes())
    assert publication["status"] == "failed"
    assert publication["arms"] == ["proposed", "current"]
    assert "published_at" not in publication


def test_empty_selection_is_evaluated_and_published_without_inventing_job_work(
    tmp_path, monkeypatch
):
    pilot = pilot_document(empty=True)
    directory, output, scratch = prepare_group(tmp_path, pilot)
    expected = observer.expected_run(pilot, "proposed", RUN_ID, 1)
    acquired = acquired_observation(scratch, expected)
    monkeypatch.setattr(observer, "wait_for_receipts", lambda *_args: acquired)
    fake_yaga(monkeypatch)
    assert observer.evaluate_group(directory, output, scratch, "proposed", RUN_ID, 1) == 0
    observation = json.loads((output / "proposed-observation.json").read_bytes())
    assert observation["cells"] == []
    assert observation["metrics"]["runner_job_seconds"] == 0
    assert observation["metrics"]["advisory_feedback_seconds"] is None
    assert json.loads((output / "publication.json").read_bytes())["status"] == "passed"


@pytest.mark.parametrize("field", ["execution_commit", "execution_tree", "run_id", "attempt"])
def test_wrong_acquired_identity_is_rejected_before_evaluation(tmp_path, monkeypatch, field):
    pilot = pilot_document()
    directory, output, scratch = prepare_group(tmp_path, pilot)
    expected = observer.expected_run(pilot, "proposed", RUN_ID, 1)
    acquired = acquired_observation(scratch, expected)
    provenance = replace(acquired.provenance, **{field: 2 if field == "attempt" else "9" * 40})
    monkeypatch.setattr(
        observer, "wait_for_receipts", lambda *_args: replace(acquired, provenance=provenance)
    )
    calls = fake_yaga(monkeypatch)
    with pytest.raises(normalization.NormalizationError, match="wrong-provenance"):
        observer.evaluate_group(directory, output, scratch, "proposed", RUN_ID, 1)
    assert calls == [] and list(output.iterdir()) == []


@pytest.mark.parametrize("attempt", [0, 2, True, 1.0, "1", None])
def test_only_integer_attempt_one_is_accepted_before_acquisition(tmp_path, monkeypatch, attempt):
    def unexpected(*_args):
        raise AssertionError("Acquisition must not run")

    monkeypatch.setattr(observer, "wait_for_receipts", unexpected)
    with pytest.raises(acquisition.AcquisitionError, match="fresh-shadow-run"):
        observer.evaluate_group(tmp_path, tmp_path, tmp_path, "proposed", RUN_ID, attempt)


@pytest.mark.parametrize(
    "aliases", [[], ["proposed", "proposed"], ["current"], ["proposed", "unknown"]]
)
def test_invalid_alias_groups_fail_before_acquisition(tmp_path, monkeypatch, aliases):
    pilot = pilot_document()
    pilot["groups"]["proposed"] = aliases
    directory, output, scratch = prepare_group(tmp_path, pilot)

    def unexpected(*_args):
        raise AssertionError("Acquisition must not run")

    monkeypatch.setattr(observer, "wait_for_receipts", unexpected)
    with pytest.raises(shadow.ShadowError, match="aliases"):
        observer.evaluate_group(directory, output, scratch, "proposed", RUN_ID, 1)


@pytest.mark.parametrize("change", ["empty", "duplicate", "binding"])
def test_alias_with_different_selection_cannot_borrow_the_group_results(
    tmp_path, monkeypatch, change
):
    pilot = pilot_document()
    if change == "empty":
        pilot["arms"]["current"]["selected"] = []
    elif change == "duplicate":
        pilot["arms"]["current"]["selected"].append(asdict(CELL))
    else:
        pilot["arms"]["current"]["selected"][0]["binding"] = "different-binding"
    directory, output, scratch = prepare_group(tmp_path, pilot)

    def unexpected(*_args):
        raise AssertionError("Acquisition must not run")

    monkeypatch.setattr(observer, "wait_for_receipts", unexpected)
    with pytest.raises(shadow.ShadowError, match="different obligations"):
        observer.evaluate_group(directory, output, scratch, "proposed", RUN_ID, 1)


def test_evaluation_rejection_never_writes_a_success_publication(tmp_path, monkeypatch):
    pilot = pilot_document(empty=True)
    directory, output, scratch = prepare_group(tmp_path, pilot)
    expected = observer.expected_run(pilot, "proposed", RUN_ID, 1)
    acquired = acquired_observation(scratch, expected)
    monkeypatch.setattr(observer, "wait_for_receipts", lambda *_args: acquired)
    fake_yaga(monkeypatch, reject=True)
    with pytest.raises(ValueError):
        observer.evaluate_group(directory, output, scratch, "proposed", RUN_ID, 1)
    assert not (output / "publication.json").exists()


def main_args(tmp_path):
    args = []
    for name in ("repo", "policy", "baseline-policy", "directory", "output", "scratch"):
        args.extend(["--" + name, str(tmp_path / name)])
    for name, value in (
        ("range", "c" * 40 + "..." + COMMIT),
        ("execution-commit", COMMIT),
        ("execution-tree", TREE),
        ("adapter-commit", COMMIT),
        ("adapter-tree", TREE),
        ("group", "proposed"),
        ("run-id", RUN_ID),
        ("attempt", "1"),
    ):
        args.extend(["--" + name, value])
    return args


@pytest.mark.parametrize("event", ["pull_request", "pull_request_target", "push", "workflow_run"])
def test_cli_rejects_other_events_without_planning_or_acquisition(tmp_path, monkeypatch, event):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)

    def unexpected(*_args):
        raise AssertionError("Planning must not run")

    monkeypatch.setattr(observer, "regenerate", unexpected)
    assert observer.main(main_args(tmp_path)) == 1
    error = json.loads((tmp_path / "output/error.json").read_bytes())
    assert error["status"] == "incomplete" and error["activation"] == "disabled"


@pytest.mark.parametrize(
    "option,value",
    [("--attempt", "2"), ("--adapter-commit", "c" * 40), ("--adapter-tree", "d" * 40)],
)
def test_cli_rejects_reruns_or_different_adapter_sources(tmp_path, monkeypatch, option, value):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")

    def unexpected(*_args):
        raise AssertionError("Planning must not run")

    monkeypatch.setattr(observer, "regenerate", unexpected)
    args = main_args(tmp_path)
    args[args.index(option) + 1] = value
    assert observer.main(args) == 1
    assert json.loads((tmp_path / "output/error.json").read_bytes())["status"] == "incomplete"


def test_cli_retains_a_bounded_safe_error_for_failed_acquisition(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setattr(observer, "regenerate", lambda *_args: None)

    def failure(*_args, **_kwargs):
        raise acquisition.AcquisitionError("private remote payload must not be printed")

    monkeypatch.setattr(observer, "evaluate_group", failure)
    assert observer.main(main_args(tmp_path)) == 1
    raw = (tmp_path / "output/error.json").read_bytes()
    assert b"private remote payload" not in raw
    assert "private remote payload" not in capsys.readouterr().out
    assert json.loads(raw)["status"] == "incomplete"


def prepare_diagnostic_cli(tmp_path, monkeypatch):
    """Keep CLI error handling real while supplying small resource-free results."""
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    pilot = pilot_document(empty=True)
    expected = observer.expected_run(pilot, "proposed", RUN_ID, 1)
    acquired = acquired_observation(tmp_path, expected)

    def regenerate(args):
        args.directory.mkdir()
        (args.directory / "pilot.json").write_bytes(shadow.encode(pilot))
        for arm in pilot["arms"]:
            (args.directory / f"pilot-{arm}-plan.json").write_bytes(shadow.encode({"arm": arm}))

    monkeypatch.setattr(observer, "regenerate", regenerate)
    monkeypatch.setattr(observer, "wait_for_receipts", lambda *_args, **_kwargs: acquired)
    fake_yaga(monkeypatch)
    return main_args(tmp_path)


@pytest.mark.parametrize(
    "stage,category,code",
    [
        ("planning", "validation", "invalid-data"),
        ("plan-validation", "validation", "invalid-data"),
        ("acquisition", "acquisition", "github-request-timeout"),
        ("evaluator-loading", "dependency", "evaluator-unavailable"),
        ("normalization", "validation", "normalization-rejected"),
        ("evaluation", "runtime", "evaluation-failed"),
        ("observation", "validation", "invalid-data"),
        ("publication", "io", "io-failed"),
    ],
)
def test_cli_identifies_each_failure_stage_without_publishing_remote_text(
    tmp_path, monkeypatch, capsys, stage, category, code
):
    args = prepare_diagnostic_cli(tmp_path, monkeypatch)
    private = "token=secret\n\x1b[31mremote body " + "X" * 10000

    def fail(*_args, **_kwargs):
        if stage == "acquisition":
            raise acquisition.AcquisitionError("github-request-timeout")
        if stage == "evaluator-loading":
            raise ImportError(private)
        if stage == "normalization":
            raise normalization.NormalizationError(private)
        if stage == "evaluation":
            raise RuntimeError(private)
        raise ValueError(private)

    if stage == "planning":
        monkeypatch.setattr(observer, "regenerate", fail)
    elif stage == "plan-validation":
        monkeypatch.setattr(observer.shadow, "read_bounded", fail)
    elif stage == "acquisition":
        monkeypatch.setattr(observer, "wait_for_receipts", fail)
    elif stage == "evaluator-loading":
        monkeypatch.setattr(observer.importlib, "import_module", fail)
    elif stage == "normalization":
        monkeypatch.setattr(normalization, "normalize_results", fail)
    elif stage == "evaluation":
        # Real YAGA InputError is a RuntimeError; keep its private message out.
        original = observer.importlib.import_module
        evaluator = original("yaga.test_planning.evaluation")
        evaluator.evaluate_results = fail
    elif stage == "observation":
        monkeypatch.setattr(observer, "make_observation", fail)
    else:
        from pathlib import Path

        original_write = Path.write_bytes

        def write(path, data):
            if path == tmp_path / "output/proposed-plan.json":
                raise OSError(private)
            return original_write(path, data)

        monkeypatch.setattr(Path, "write_bytes", write)

    assert observer.main(args) == 1
    raw = (tmp_path / "output/diagnostics.json").read_bytes()
    diagnostic = json.loads(raw)
    assert (diagnostic["stage"], diagnostic["category"], diagnostic["code"]) == (
        stage,
        category,
        code,
    )
    assert diagnostic["status"] == "incomplete"
    assert diagnostic["authority"] == "diagnostic-only"
    assert diagnostic["activation"] == "disabled"
    assert diagnostic["run_id"] == RUN_ID
    assert diagnostic["source"]["execution_commit"] == COMMIT
    assert diagnostic["source"]["execution_tree"] == TREE
    assert len(raw) <= observer.MAX_DIAGNOSTIC_BYTES
    assert b"token=" not in raw and b"remote body" not in raw
    assert "token=" not in capsys.readouterr().out
    assert not (tmp_path / "output/publication.json").exists()


def test_second_alias_failure_identifies_arm_and_cannot_publish_partial_success(
    tmp_path, monkeypatch
):
    args = prepare_diagnostic_cli(tmp_path, monkeypatch)
    original = observer.make_observation

    def second(pilot, arm, *values):
        if arm == "current":
            raise ValueError("private second arm payload")
        return original(pilot, arm, *values)

    monkeypatch.setattr(observer, "make_observation", second)
    assert observer.main(args) == 1
    diagnostic = json.loads((tmp_path / "output/diagnostics.json").read_bytes())
    assert diagnostic["stage"] == "observation" and diagnostic["arm"] == "current"
    assert (tmp_path / "output/proposed-evaluation.json").is_file()
    assert not (tmp_path / "output/publication.json").exists()


def test_diagnostic_output_failure_preserves_nonzero_exit_and_safe_console(
    tmp_path, monkeypatch, capsys
):
    from pathlib import Path

    args = prepare_diagnostic_cli(tmp_path, monkeypatch)
    original_write = Path.write_bytes

    def write(path, data):
        if path.parent == tmp_path / "output":
            raise OSError("private filesystem details")
        return original_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", write)
    assert observer.main(args) == 1
    console = capsys.readouterr().out
    assert "stage=publication category=io code=io-failed" in console
    assert "private filesystem" not in console
    assert not (tmp_path / "output/publication.json").exists()


@pytest.mark.parametrize(
    "value",
    ["secret\n" * 10000, "\x1b[31mprivate", "a" * 20000, "", None],
    ids=["oversized-private", "terminal-control", "oversized-hex", "empty", "null"],
)
def test_diagnostic_identity_fields_never_echo_unbounded_or_malformed_input(value):
    args = SimpleNamespace(
        group=value,
        run_id=value,
        attempt=value,
        execution_commit=value,
        execution_tree=value,
        adapter_commit=value,
        adapter_tree=value,
    )
    report = observer.ObserverDiagnostics().failure(args, RuntimeError("secret"))
    assert report["group"] is None and report["run_id"] is None and report["attempt"] is None
    assert set(report["source"].values()) == {None}
    raw = shadow.encode(report)
    assert len(raw) < observer.MAX_DIAGNOSTIC_BYTES and b"secret" not in raw


def test_pending_deadline_is_retained_as_acquisition_diagnostic(tmp_path, monkeypatch):
    real_wait = observer.wait_for_receipts
    args = prepare_diagnostic_cli(tmp_path, monkeypatch)
    clock = FakeClock()
    monkeypatch.setattr(observer, "MAX_POLLS", 2)

    def pending(*_args, **_kwargs):
        raise acquisition.AcquisitionPending("selected-job-incomplete")

    monkeypatch.setattr(
        observer,
        "wait_for_receipts",
        lambda expected, destination, **_kwargs: real_wait(
            expected, destination, acquire=pending, clock=clock, sleep=clock.sleep
        ),
    )
    assert observer.main(args) == 1
    report = json.loads((tmp_path / "output/diagnostics.json").read_bytes())
    assert report["stage"] == "acquisition" and report["code"] == "shadow-observation-deadline"
    assert clock.sleeps == [60]
