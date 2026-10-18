"""Explicit advisory plans and fixed publication groups for a full shadow run."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
from pathlib import Path

from scripts.test_planning_shadow import (
    JOBS,
    ShadowError,
    baseline_selection,
    decode,
    encode,
    read_bounded,
    registry_for,
)

ARMS = ("current", "proposed", "full")
YAGA_SOURCE = "yaga-cli==0.3.0"


def selected_cells(plan: dict) -> list[dict[str, str]]:
    bindings = {suite["id"]: suite["binding"] for suite in plan["plan"]["inventory"]}
    return [
        {"suite": item["suite"], "variant": variant, "binding": bindings[item["suite"]]}
        for item in plan["plan"]["selected"]
        for variant in item["variants"]
    ]


def baseline_policy(inventory: list[dict], selected: list[str], baseline_sha256: str) -> bytes:
    """Materialize this change's existing selector result, never a new CI policy."""
    if type(baseline_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", baseline_sha256) is None:
        raise ShadowError("Invalid baseline policy digest")
    if selected != sorted(set(selected)) or not set(selected) <= set(JOBS):
        raise ShadowError("Invalid materialized baseline selection")
    if len(inventory) != len(JOBS) or {item["id"] for item in inventory} != {
        "qualification." + name for name in JOBS
    }:
        raise ShadowError("Invalid baseline catalog")
    lines = [
        "# Materialized existing selector for ONE explicit source comparison.",
        "# Not a reusable policy, changed required gate, or proposed selection.",
        "# Original baseline policy SHA-256: " + baseline_sha256,
        "test-policy-version = 1",
        'no-impact = ["**"]',
        "[profiles.current]",
        'mode = "selective"',
        "always = " + json.dumps(["qualification." + name for name in selected]),
    ]
    for suite in inventory:
        lines.append("[[suites]]")
        for key in ("id", "binding", "tags", "variants", "needs", "requires"):
            lines.append(key + " = " + json.dumps(suite[key], ensure_ascii=True))
    return ("\n".join(lines) + "\n").encode("utf-8")


def publication_groups(plans: dict[str, dict]) -> dict[str, list[str]]:
    """Identical obligations share one actual publication, avoiding clock skew."""
    groups: dict[str, list[str]] = {}
    by_cells = {}
    for arm in ("full", "proposed", "current"):
        key = tuple(
            sorted(
                (cell["suite"], cell["variant"], cell["binding"])
                for cell in selected_cells(plans[arm])
            )
        )
        group = by_cells.setdefault(key, arm)
        groups.setdefault(group, []).append(arm)
    return groups


def prepare(directory: Path, policy_path: Path, baseline_path: Path) -> dict:
    """Validate independently generated plans and add bounded pilot artifacts."""
    diagnostics = importlib.import_module("yaga.test_planning.diagnostics")
    reporting = importlib.import_module("yaga.test_planning.reporting")
    models = importlib.import_module("yaga.test_planning.reports")
    planner = importlib.import_module("yaga.test_planning.planner")
    policies = importlib.import_module("yaga.test_planning.policy")
    handoffs = importlib.import_module("yaga.test_planning.handoff")
    proposed_document = decode(read_bounded(directory / "proposed-plan.json"))
    proposed = diagnostics.parse_plan_document(proposed_document)
    comparison = decode(read_bounded(directory / "comparison.json"))
    adapter = comparison["adapter_source"]
    registry, manifest = registry_for(adapter["commit"], adapter["tree"])
    if decode(read_bounded(directory / "executor-manifest.json")) != manifest:
        raise ShadowError("Pilot executor manifest changed")
    policy_bytes = read_bounded(policy_path)
    policy_sha = hashlib.sha256(policy_bytes).hexdigest()
    if proposed.policy_sha256 != policy_sha or proposed.bindings_sha256 != registry.sha256:
        raise ShadowError("Pilot policy or binding identity changed")
    if proposed.source.base_commit is None:
        raise ShadowError("Pilot requires an explicit complete change comparison")
    baseline_bytes = read_bounded(baseline_path)
    selected = baseline_selection(baseline_bytes, proposed.selection.changed_paths)
    baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
    if comparison["baseline_policy_sha256"] != baseline_sha:
        raise ShadowError("Pilot baseline identity changed")
    current_bytes = baseline_policy(proposed_document["plan"]["inventory"], selected, baseline_sha)
    current = models.PlannedTests(
        policy_sha256=hashlib.sha256(current_bytes).hexdigest(),
        bindings_sha256=registry.sha256,
        source=proposed.source,
        selection=planner.plan_tests(
            policies.parse_test_policy(current_bytes),
            profile="current",
            changed_paths=proposed.selection.changed_paths,
        ),
    )
    full = models.PlannedTests(
        policy_sha256=policy_sha,
        bindings_sha256=registry.sha256,
        source=proposed.source,
        selection=planner.plan_tests(
            policies.parse_test_policy(policy_bytes),
            profile="integration",
            changed_paths=proposed.selection.changed_paths,
        ),
    )
    plans = {"current": current, "proposed": proposed, "full": full}
    documents = {arm: reporting.test_plan_document(plan) for arm, plan in plans.items()}
    for plan in plans.values():
        handoffs.create_handoff(
            plan,
            registry,
            expected_policy_sha256=plan.policy_sha256,
            expected_execution_commit=proposed.source.execution_commit,
            expected_execution_tree=proposed.source.execution_tree,
        )
    groups = publication_groups(documents)
    pilot = {
        "test-shadow-pilot-version": 1,
        "activation": "disabled",
        "method": "shared-full-execution-publication",
        "source": proposed_document["plan"]["source"],
        "adapter_source": adapter,
        "bindings_sha256": registry.sha256,
        "baseline_policy_sha256": baseline_sha,
        "groups": groups,
        "matrix": {"include": [{"group": group} for group in groups]},
        "arms": {
            arm: {
                "policy_sha256": document["plan"]["policy_sha256"],
                "plan_digest": document["plan_digest"],
                "selected": selected_cells(document),
            }
            for arm, document in documents.items()
        },
    }
    # Validate all outputs before replacing any existing full-plan artifact.
    encoded = {f"pilot-{arm}-plan.json": encode(doc) for arm, doc in documents.items()}
    encoded["pilot.json"] = encode(pilot)
    encoded["pilot-current-policy.toml"] = current_bytes
    for name, raw in encoded.items():
        (directory / name).write_bytes(raw)
    return pilot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("directory", "policy", "baseline-policy"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        pilot = prepare(args.directory, args.policy, args.baseline_policy)
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
                stream.write("matrix=" + encode(pilot["matrix"]).decode() + "\n")
        print(encode(pilot).decode())
    except (RuntimeError, ValueError, TypeError, KeyError, OSError, ImportError, RecursionError):
        print("Pilot planning failed; no execution or activation authorized")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
