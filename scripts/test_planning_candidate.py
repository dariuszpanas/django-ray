"""Evaluate one reviewed historical qualification exception without activating it."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import re
from pathlib import Path

from scripts import test_planning_shadow as shadow

REPLAY_SHA256 = "88c3f957023e6cb687bfb76e873a833e82c2be7a8aad14b9124b4cd554beceb8"
BASELINE_SHA256 = "58665248ab9358b2e9995f4d5f17e9e5d3f536175032a95f20f5fdcb41788607"
# The retained replay read a CRLF checkout; the audited Git blob uses LF.
# Accept only these reviewed byte identities, without normalizing caller input.
SOURCE_BASELINE_SHA256 = "fecbee6c70cd29913218e0c0a3c973ca44c3a14734fd53807d2111f80f99dcb4"
AUDITED_CONSUMER = {
    "commit": "f5781e70ad30624b80ac6e2dbcc443e40f93d4bb",
    "tree": "90ed09d9bf0f3a56269661853d7cbf565bb168b2",
}
REPLAY_ADAPTER = {
    "commit": "80c632b0f9f708a57c1d1d02b3a7a41950338e68",
    "tree": "833404c8ee1022537b20558b82cbc839dc33bc57",
}
REPLAY_POLICY_SHA256 = "6bc4c9843c9c5d063ed76b3d7175d87611c4b464519d0f3994fa2c596ad108ab"
REPLAY_BINDINGS_SHA256 = "38002ca70afda5f14556ea7ab7f96b85de74bf14a1529e575fab5932e58e10a6"
REVIEWED_REVISION = {
    "number": 621,
    "base": "b1b7041ed7c5f3d847b199002bfc59295ecbab15",
    "head": "60af8d6e4b44e0066562a8134dfaf5cec56dba32",
    "tree": "7f29706be610892d760ef7362609f562840ca0e4",
}
REVIEWED_PATHS = (
    ".github/test-suite-taxonomy.json",
    "Makefile",
    "adrs/adr-0006-bounded-operator-diagnosis.md",
    "docs/reference/cli.md",
    "docs/reference/doctor.md",
    "scripts/verify_doctor_wheel.py",
    "scripts/verify_wheel.py",
    "src/django_ray/doctor/__init__.py",
    "src/django_ray/doctor/__main__.py",
    "src/django_ray/doctor/contracts.py",
    "src/django_ray/doctor/probe.py",
    "src/django_ray/doctor/runner.py",
    "src/django_ray/doctor/supervisor.py",
    "src/django_ray/management/commands/django_ray_doctor.py",
    "tests/integration/test_doctor_postgresql.py",
    "tests/unit/test_doctor.py",
    "tests/unit/test_doctor_supervision.py",
    "tests/unit/test_verify_doctor_wheel.py",
    "zensical.toml",
)
COHORT_SIZE = 31


def require(condition, message):
    if not condition:
        raise shadow.ShadowError(message)


def candidate_selection(revision, changed_paths, baseline):
    """A fixed source exception cannot generalize to similar paths or future code."""
    require(
        type(revision) is dict
        and set(revision) in (set(REVIEWED_REVISION), {*REVIEWED_REVISION, "merged_at"})
        and type(revision["number"]) is int
        and 0 < revision["number"] <= 2**31 - 1,
        "Invalid candidate revision",
    )
    for key in ("base", "head", "tree"):
        shadow.oid(revision[key])
    require(
        type(changed_paths) in (list, tuple)
        and len(changed_paths) <= 3000
        and all(
            type(path) is str
            and 0 < len(path.encode("utf-8")) <= 4096
            and "\\" not in path
            and all(part not in {"", ".", ".."} for part in path.split("/"))
            and not any(ord(character) < 32 or ord(character) == 127 for character in path)
            for path in changed_paths
        )
        and len(set(changed_paths)) == len(changed_paths),
        "Invalid or incomplete candidate paths",
    )
    require(
        type(baseline) is list
        and all(type(name) is str and name in shadow.JOBS for name in baseline)
        and baseline == sorted(set(baseline)),
        "Invalid candidate baseline selection",
    )
    if (
        all(revision.get(key) == value for key, value in REVIEWED_REVISION.items())
        and tuple(changed_paths) == REVIEWED_PATHS
        and baseline == ["application"]
    ):
        return []
    return list(baseline)


def evaluate(
    replay_bytes,
    baseline_bytes,
    *,
    expected_replay_sha256,
    expected_consumer_commit,
    expected_consumer_tree,
):
    """Check the retained cohort, rematch baseline paths, and emit a counterfactual."""
    require(
        type(replay_bytes) is bytes
        and len(replay_bytes) <= shadow.MAX_DOCUMENT_BYTES
        and type(baseline_bytes) is bytes
        and len(baseline_bytes) <= shadow.MAX_BASELINE_BYTES,
        "Candidate inputs exceed their fixed byte limits",
    )
    require(
        expected_replay_sha256 == REPLAY_SHA256
        and hashlib.sha256(replay_bytes).hexdigest() == REPLAY_SHA256,
        "Candidate requires the independently pinned reviewed replay",
    )
    input_baseline_sha256 = hashlib.sha256(baseline_bytes).hexdigest()
    require(
        input_baseline_sha256 in {BASELINE_SHA256, SOURCE_BASELINE_SHA256},
        "Candidate baseline policy changed",
    )
    consumer = {"commit": expected_consumer_commit, "tree": expected_consumer_tree}
    require(consumer == AUDITED_CONSUMER, "Candidate consumer snapshot has not been reviewed")
    document = shadow.decode(replay_bytes)
    require(
        type(document) is dict
        and set(document)
        == {
            "activation",
            "adapter_file_sha256",
            "cohort",
            "experiment_version",
            "mode",
            "observations",
            "replay_file_sha256",
            "repository",
            "summary",
        },
        "Invalid retained replay envelope",
    )
    require(
        document["activation"] == "disabled"
        and type(document["experiment_version"]) is int
        and document["experiment_version"] == 1
        and document["mode"] == "historical-selection-replay"
        and document["repository"] == "dariuszpanas/django-ray",
        "Invalid retained replay authority",
    )
    for field in ("adapter_file_sha256", "replay_file_sha256"):
        require(
            type(document[field]) is str
            and re.fullmatch(r"[0-9a-f]{64}", document[field]) is not None,
            "Invalid retained generator digest",
        )
    cohort, observations = document["cohort"], document["observations"]
    require(
        type(cohort) is list
        and type(observations) is list
        and len(cohort) == len(observations) == COHORT_SIZE,
        "Retained cohort must contain exactly 31 complete observations",
    )
    summary = document["summary"]
    require(
        type(summary) is dict
        and summary.get("revisions") == COHORT_SIZE
        and type(summary.get("test_runtime_observations")) is int
        and summary["test_runtime_observations"] == 0,
        "Invalid retained replay summary",
    )
    require(
        importlib.metadata.version("yaga-cli") == "0.3.0", "Candidate requires reviewed YAGA 0.3.0"
    )
    decoder = importlib.import_module("yaga.test_planning.diagnostics")
    seen = set()
    rows: list[dict] = []
    for revision, observation in zip(cohort, observations, strict=True):
        require(
            type(revision) is dict
            and set(revision) == {"number", "base", "head", "tree", "merged_at"},
            "Invalid cohort revision",
        )
        number = revision["number"]
        require(
            type(number) is int and 0 < number <= 2**31 - 1 and number not in seen,
            "Invalid or duplicate cohort revision",
        )
        seen.add(number)
        for key in ("base", "head", "tree"):
            shadow.oid(revision[key])
        require(
            type(revision["merged_at"]) is str
            and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", revision["merged_at"]) is not None,
            "Invalid revision timestamp",
        )
        require(
            type(observation) is dict
            and set(observation) == {"revision", "comparison", "plan"}
            and observation["revision"] == revision,
            "Replay observation and cohort differ",
        )
        decoder.parse_plan_document(observation["plan"])
        plan = observation["plan"]["plan"]
        comparison = observation["comparison"]
        source = {
            "base_commit": revision["base"],
            "comparison_commit": revision["base"],
            "head_commit": revision["head"],
            "execution_commit": revision["head"],
            "execution_tree": revision["tree"],
        }
        require(
            type(comparison) is dict and plan["source"] == comparison.get("source") == source,
            "Replay source differs from cohort",
        )
        require(
            comparison.get("adapter_source") == REPLAY_ADAPTER
            and comparison.get("baseline_policy_sha256") == BASELINE_SHA256
            and comparison.get("policy_sha256") == plan["policy_sha256"] == REPLAY_POLICY_SHA256
            and comparison.get("bindings_sha256")
            == plan["bindings_sha256"]
            == REPLAY_BINDINGS_SHA256
            and comparison.get("plan_digest") == observation["plan"]["plan_digest"]
            and comparison.get("activation") == "disabled"
            and comparison.get("runtime_observation") == "not-observed",
            "Retained comparison identities differ",
        )
        paths = plan["changed_paths"]
        require(
            type(paths) is list and len(paths) <= 3000 and len(set(paths)) == len(paths),
            "Incomplete or duplicate changed paths",
        )
        baseline = shadow.baseline_selection(baseline_bytes, tuple(paths))
        require(
            baseline == comparison.get("baseline_selected"),
            "Recomputed baseline differs from retained comparison",
        )
        selected = candidate_selection(revision, paths, baseline)
        rows.append(
            {
                "revision": revision,
                "plan_digest": observation["plan"]["plan_digest"],
                "changed_path_count": len(paths),
                "baseline_selected": baseline,
                "candidate_selected": selected,
                "reviewed_exception": selected != baseline,
            }
        )
    report = {
        "test-planning-candidate-version": 1,
        "authority": "historical-counterfactual-only",
        "activation": "disabled",
        "candidate": "reviewed-doctor-revision-621",
        "audited_consumer_source": consumer,
        "retained_replay_adapter_source": dict(REPLAY_ADAPTER),
        "replay_sha256": REPLAY_SHA256,
        "baseline_policy_sha256": input_baseline_sha256,
        "retained_replay_baseline_policy_sha256": BASELINE_SHA256,
        "git_source_baseline_policy_sha256": SOURCE_BASELINE_SHA256,
        "evaluator": "yaga-cli==0.3.0",
        "future_coverage_review": "required",
        "observations": rows,
        "summary": {
            "revisions": len(rows),
            "baseline_suite_selections": sum(len(row["baseline_selected"]) for row in rows),
            "candidate_suite_selections": sum(len(row["candidate_selected"]) for row in rows),
            "baseline_variant_cells": sum(
                len(shadow.VARIANTS[name]) for row in rows for name in row["baseline_selected"]
            ),
            "candidate_variant_cells": sum(
                len(shadow.VARIANTS[name]) for row in rows for name in row["candidate_selected"]
            ),
            "unchanged_revisions": sum(not row["reviewed_exception"] for row in rows),
            "feedback_time_savings_seconds": None,
            "runner_minute_savings": None,
            "new_runtime_observations": 0,
        },
    }
    shadow.encode(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            f"Requires retained replay SHA-256 {REPLAY_SHA256}; audited consumer "
            f"commit {AUDITED_CONSUMER['commit']} and tree {AUDITED_CONSUMER['tree']}. "
            "Run as python -m scripts.test_planning_candidate. Selection remains disabled."
        ),
    )
    for name in ("replay", "baseline-policy", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("expected-replay-sha256", "expected-consumer-commit", "expected-consumer-tree"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate(
            shadow.read_bounded(args.replay),
            shadow.read_bounded(args.baseline_policy, shadow.MAX_BASELINE_BYTES),
            expected_replay_sha256=args.expected_replay_sha256,
            expected_consumer_commit=args.expected_consumer_commit,
            expected_consumer_tree=args.expected_consumer_tree,
        )
        with args.output.open("xb") as stream:
            stream.write(shadow.encode(report))
    except (RuntimeError, ValueError, TypeError, KeyError, OSError, ImportError, RecursionError):
        print("Historical candidate could not be evaluated; no selection was activated")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
