"""Compare an explicit Git range with an inactive doctor path candidate."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
from pathlib import Path, PureWindowsPath

from scripts import test_planning_candidate as historical
from scripts import test_planning_shadow as shadow

SOURCE_PATHS = tuple(path for path in historical.REVIEWED_PATHS if path.startswith("src/"))
COMPANION_PATHS = (
    "docs/reference/doctor.md",
    "scripts/verify_doctor_wheel.py",
    "tests/integration/test_doctor_postgresql.py",
    "tests/unit/test_doctor.py",
    "tests/unit/test_doctor_supervision.py",
    "tests/unit/test_verify_doctor_wheel.py",
)
MAX_PATHS = 3000
MAX_PATH_BYTES = 4096
ELIGIBLE_REASON = "path-eligible-review-required"


def baseline_digest(raw: bytes) -> str:
    if type(raw) is not bytes or len(raw) > shadow.MAX_BASELINE_BYTES:
        raise shadow.ShadowError("Candidate baseline exceeds its byte limit")
    digest = hashlib.sha256(raw).hexdigest()
    if digest not in {historical.BASELINE_SHA256, historical.SOURCE_BASELINE_SHA256}:
        raise shadow.ShadowError("Candidate baseline policy changed")
    return digest


def validated_paths(changed_paths) -> tuple[str, ...]:
    if type(changed_paths) not in (list, tuple) or len(changed_paths) > MAX_PATHS:
        raise shadow.ShadowError("Invalid or incomplete candidate paths")
    for path in changed_paths:
        if (
            type(path) is not str
            or not 0 < len(path) <= MAX_PATH_BYTES
            or "\\" in path
            or PureWindowsPath(path).drive
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise shadow.ShadowError("Invalid or incomplete candidate paths")
        try:
            size = len(path.encode("utf-8"))
        except UnicodeError:
            raise shadow.ShadowError("Invalid or incomplete candidate paths") from None
        if size > MAX_PATH_BYTES:
            raise shadow.ShadowError("Invalid or incomplete candidate paths")
    if len(set(changed_paths)) != len(changed_paths):
        raise shadow.ShadowError("Invalid or incomplete candidate paths")
    return tuple(sorted(changed_paths))


def compare(changed_paths, baseline_bytes: bytes) -> dict:
    """Recompute the baseline; path eligibility alone never proves safe omission."""
    baseline_digest(baseline_bytes)
    paths = validated_paths(changed_paths)
    baseline = shadow.baseline_selection(baseline_bytes, paths)
    reason = "baseline-not-application-only"
    if not set(paths) <= set(SOURCE_PATHS + COMPANION_PATHS):
        reason = "outside-reviewed-doctor-paths"
    elif not set(paths).intersection(SOURCE_PATHS):
        reason = "no-reviewed-doctor-source"
    elif baseline == ["application"]:
        reason = ELIGIBLE_REASON
    result = {
        "baseline_selected": baseline,
        "candidate_selected": [] if reason == ELIGIBLE_REASON else list(baseline),
        "reason": reason,
        "changed_paths": list(paths),
    }
    shadow.encode(result)
    return result


def committed_structure(repo: Path, *, base: str, head: str, expected_tree: str) -> dict:
    """Read metadata only; the pinned typed reader validates complete tree records."""
    reader = importlib.import_module("yaga.modes.git")
    models = importlib.import_module("yaga.modes.models")
    paths = tuple(sorted(SOURCE_PATHS + COMPANION_PATHS))
    endpoints, entries = {}, {}
    for label, commit in (("base", base), ("head", head)):
        selection = reader.read_mode_selection(repo, commit)
        # ModeSelection validates complete, bounded, sorted, unique leaf metadata,
        # including topology, supported mode/type pairs and every object ID.
        if type(selection) is not models.ModeSelection:
            raise shadow.ShadowError("Invalid candidate committed structure")
        tree = shadow.oid(selection.tree_sha)
        if selection.commit_sha != commit or (label == "head" and tree != expected_tree):
            raise shadow.ShadowError("Candidate structure differs from the expected source")
        endpoints[label] = {"commit": commit, "tree": tree}
        entries[label] = {
            entry.path: {"mode": entry.mode, "type": entry.object_type, "oid": entry.oid}
            for entry in selection.entries
            if entry.path in paths
        }
    rows = [
        {"path": path, "base": entries["base"].get(path), "head": entries["head"].get(path)}
        for path in paths
    ]
    preserved = all(
        row[label] is not None and row[label]["mode"] == "100644" and row[label]["type"] == "blob"
        for row in rows
        for label in ("base", "head")
    )
    return {
        "status": "preserved" if preserved else "not-preserved",
        **endpoints,
        "paths": rows,
    }


def evaluate_range(repo, baseline_bytes: bytes, *, base, head, expected_tree) -> dict:
    """Bind an offline comparison to one explicit two-dot range and expected tree."""
    digest = baseline_digest(baseline_bytes)
    expected = {
        "base_commit": shadow.oid(base),
        "head_commit": shadow.oid(head),
        "comparison_commit": shadow.oid(base),
        "execution_commit": shadow.oid(head),
        "execution_tree": shadow.oid(expected_tree),
    }
    if importlib.metadata.version("yaga-cli") != "0.3.0":
        raise shadow.ShadowError("Candidate requires the reviewed YAGA version")
    collector = importlib.import_module("yaga.test_planning.git")
    collected = collector.collect_test_source(Path(repo), revision_range=f"{base}..{head}")
    source = getattr(collected, "source", None)
    if any(getattr(source, key, None) != value for key, value in expected.items()):
        raise shadow.ShadowError("Candidate source differs from the expected range")
    comparison = compare(getattr(collected, "changed_paths", None), baseline_bytes)
    path_eligible = comparison["reason"] == ELIGIBLE_REASON
    structure = {"status": "not-evaluated", "base": None, "head": None, "paths": []}
    structure_sha256 = None
    if path_eligible:
        structure = committed_structure(
            Path(repo), base=base, head=head, expected_tree=expected_tree
        )
        structure_sha256 = hashlib.sha256(shadow.encode(structure)).hexdigest()
        if structure["status"] != "preserved":
            comparison["candidate_selected"] = list(comparison["baseline_selected"])
            comparison["reason"] = "doctor-structure-not-preserved"
    inventory = {"sources": list(SOURCE_PATHS), "companions": list(COMPANION_PATHS)}
    report = {
        "experiment_version": 1,
        "authority": "offline-counterfactual-only",
        "activation": "disabled",
        "comparison_kind": "explicit-two-dot-range",
        "range": f"{base}..{head}",
        "source": expected,
        "evaluator": "yaga-cli==0.3.0",
        "baseline_policy_sha256": digest,
        "rule_path_inventory": inventory,
        "rule_path_inventory_sha256": hashlib.sha256(shadow.encode(inventory)).hexdigest(),
        "path_eligible": path_eligible,
        "candidate_eligible": path_eligible and structure["status"] == "preserved",
        "structure": structure,
        "structure_sha256": structure_sha256,
        "dependency_review": "required",
        "coverage_review": "required",
        **comparison,
        "summary": {
            "baseline_suite_selections": len(comparison["baseline_selected"]),
            "candidate_suite_selections": len(comparison["candidate_selected"]),
            "baseline_variant_cells": sum(
                len(shadow.VARIANTS[name]) for name in comparison["baseline_selected"]
            ),
            "candidate_variant_cells": sum(
                len(shadow.VARIANTS[name]) for name in comparison["candidate_selected"]
            ),
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
            "Run as python -m scripts.test_planning_doctor_candidate. "
            "Path eligibility requires dependency and coverage review; selection stays disabled."
        ),
    )
    for name in ("repo", "baseline-policy", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("base", "head", "expected-tree"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate_range(
            args.repo,
            shadow.read_bounded(args.baseline_policy, shadow.MAX_BASELINE_BYTES),
            base=args.base,
            head=args.head,
            expected_tree=args.expected_tree,
        )
        raw = shadow.encode(report)
        with args.output.open("xb") as stream:
            stream.write(raw)
    except (RuntimeError, ValueError, TypeError, KeyError, OSError, ImportError, RecursionError):
        print("Doctor candidate could not be evaluated; no selection was activated")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
