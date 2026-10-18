"""Advisory fixed-workflow comparison; never changes required CI selection."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import time
from pathlib import Path

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_BASELINE_BYTES = 256 * 1024
JOBS = ("application", "native", "latency", "cancellation", "monitoring")
WORKFLOWS = {
    "application": "application-qualification",
    "native": "upgrade-native-qualification",
    "latency": "latency-qualification",
    "cancellation": "cancellation-qualification",
    "monitoring": "monitoring-qualification",
}
VARIANTS = {
    name: ("core-loss", "jobs", "jobs-crash", "native") if name == "native" else ("default",)
    for name in JOBS
}
CAPABILITIES = {
    "application": ("chainsaw", "docker", "helm", "kind", "linux"),
    "native": ("docker", "linux"),
    "latency": ("docker", "linux"),
    "cancellation": ("docker", "linux"),
    "monitoring": ("docker", "helm", "kind", "linux"),
}
OUTPUT_KEYS = {
    "full_shadow",
    "execution_commit",
    "execution_tree",
    "policy_sha256",
    "bindings_sha256",
    "plan_digest",
}


class ShadowError(RuntimeError):
    """A fixed diagnostic safe to publish without arbitrary input content."""


def encode(document: object) -> bytes:
    raw = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ShadowError("Shadow document exceeds its byte limit")
    return raw


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ShadowError("Duplicate shadow input key")
        result[key] = value
    return result


def reject_constant(value: str):
    raise ShadowError("Non-finite shadow JSON number")


def decode(raw: bytes, maximum: int = MAX_DOCUMENT_BYTES):
    if len(raw) > maximum:
        raise ShadowError("Shadow input exceeds its byte limit")
    try:
        return json.loads(raw, object_pairs_hook=object_pairs, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ShadowError("Invalid shadow JSON input") from None


def read_bounded(path: Path, maximum: int = MAX_DOCUMENT_BYTES) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ShadowError("Shadow input exceeds its byte limit")
    return raw


def oid(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ShadowError("Expected an exact Git object identity")
    return value


def baseline_selection(raw: bytes, changed_paths: tuple[str, ...]) -> list[str]:
    # Import this repository's current selector, not code from historical commits.
    from scripts.qualification_gate import select

    policy = decode(raw, MAX_BASELINE_BYTES)
    if (
        not isinstance(policy, list)
        or len(policy) != len(JOBS)
        or any(not isinstance(item, dict) for item in policy)
        or {item.get("id") for item in policy} != set(JOBS)
    ):
        raise ShadowError("The baseline must contain every existing qualification")
    for entry in policy:
        name = entry["id"]
        if entry.get("workflow") != WORKFLOWS[name] + ".yml":
            raise ShadowError("Baseline workflow differs from the fixed registry")
        paths = entry.get("paths")
        if (
            not isinstance(paths, list)
            or not 1 <= len(paths) <= 128
            or any(not isinstance(path, str) or not 1 <= len(path) <= 4096 for path in paths)
        ):
            raise ShadowError("Invalid bounded baseline path inventory")
    return sorted(entry["id"] for entry in select(policy, changed_paths))


def registry_for(adapter_commit: str, adapter_tree: str):
    """Bind reviewed fixed dispatch definitions to their entire immutable source tree."""
    handoff = importlib.import_module("yaga.test_planning.handoff")
    source = {"commit": oid(adapter_commit), "tree": oid(adapter_tree)}
    definitions = []
    executors = []
    for name in JOBS:
        definition = {
            "adapter_source": source,
            "workflow": ".github/workflows/" + WORKFLOWS[name] + ".yml",
            "dispatch": name,
            "variants": list(VARIANTS[name]),
            "capabilities": list(CAPABILITIES[name]),
            "execution": "existing-workflow-once",
            "acceptance": "standard",
        }
        definitions.append(definition)
        executors.append(
            handoff.SuiteExecutor(
                suite="qualification." + name,
                binding=WORKFLOWS[name],
                dispatch=name,
                variants=VARIANTS[name],
                capabilities=CAPABILITIES[name],
                definition_sha256=hashlib.sha256(encode(definition)).hexdigest(),
            )
        )
    registry = handoff.ExecutorRegistry(tuple(executors))
    return registry, {"registry": registry.document(), "definitions": definitions}


def generate(
    repository: Path,
    *,
    policy_path: Path,
    baseline_path: Path,
    revision_range: str,
    execution_commit: str,
    execution_tree: str,
    adapter_commit: str,
    adapter_tree: str,
    full_shadow: bool = False,
) -> dict[str, dict]:
    """Return bounded documents for explicit inputs, with no workflow dispatch or writes."""
    oid(execution_commit)
    oid(execution_tree)
    registry, manifest = registry_for(adapter_commit, adapter_tree)
    service = importlib.import_module("yaga.test_planning.service")
    reports = importlib.import_module("yaga.test_planning.reporting")
    handoff = importlib.import_module("yaga.test_planning.handoff")
    policy_bytes = read_bounded(policy_path, MAX_BASELINE_BYTES)
    policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
    baseline_bytes = read_bounded(baseline_path, MAX_BASELINE_BYTES)
    started = time.perf_counter()
    planned = service.create_test_plan(
        repository,
        policy_path=policy_path,
        profile="development",
        revision_range=revision_range,
        bindings_sha256=registry.sha256,
    )
    proposal = handoff.create_handoff(
        planned,
        registry,
        expected_policy_sha256=policy_sha256,
        expected_execution_commit=execution_commit,
        expected_execution_tree=execution_tree,
    )
    baseline = baseline_selection(baseline_bytes, planned.selection.changed_paths)
    selected = sorted(name for name, wanted in proposal["dispatch"].items() if wanted)
    missing = sorted(set(baseline) - set(selected))
    additional = sorted(set(selected) - set(baseline))
    comparison = {
        "test-shadow-version": 1,
        "status": "omission" if missing else "compared",
        "activation": "disabled",
        "adapter_source": {"commit": adapter_commit, "tree": adapter_tree},
        "source": proposal["source"],
        "policy_sha256": policy_sha256,
        "baseline_policy_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "bindings_sha256": registry.sha256,
        "plan_digest": proposal["plan_digest"],
        "baseline_selected": baseline,
        "planned_selected": selected,
        "missing": missing,
        "additional": additional,
        "selected_cells": proposal["cells"],
        "effective_mode": proposal["effective_mode"],
        "planning_seconds": round(time.perf_counter() - started, 6),
        "runner_minutes": None,
        "critical_path_seconds": None,
        "excluded_failures": None,
        "runtime_observation": "not-observed",
    }
    documents = {
        "comparison.json": comparison,
        "proposed-plan.json": reports.test_plan_document(planned),
        "proposed-handoff.json": proposal,
        "executor-manifest.json": manifest,
    }
    if full_shadow:
        full = service.create_test_plan(
            repository,
            policy_path=policy_path,
            profile="integration",
            revision=execution_commit,
            bindings_sha256=registry.sha256,
        )
        full_handoff = handoff.create_handoff(
            full,
            registry,
            expected_policy_sha256=policy_sha256,
            expected_execution_commit=execution_commit,
            expected_execution_tree=execution_tree,
        )
        if full_handoff["dispatch"] != dict.fromkeys(JOBS, True):
            raise ShadowError("Full shadow must dispatch every fixed qualification")
        documents["full-plan.json"] = reports.test_plan_document(full)
        documents["full-handoff.json"] = full_handoff
    # Do not leave partial success documents if any complete report is over budget.
    for document in documents.values():
        encode(document)
    return documents


def job_outputs(documents: dict[str, dict], *, full_shadow: bool) -> dict[str, str]:
    comparison = documents["comparison.json"]
    if comparison["missing"]:
        raise ShadowError("The proposed selection omits existing qualification")
    selected = documents["full-handoff.json" if full_shadow else "proposed-handoff.json"]
    result = {
        "full_shadow": "true" if full_shadow else "false",
        "execution_commit": selected["source"]["execution_commit"],
        "execution_tree": selected["source"]["execution_tree"],
        "policy_sha256": selected["policy_sha256"],
        "bindings_sha256": selected["bindings_sha256"],
        "plan_digest": selected["plan_digest"],
    }
    validate_outputs(result)
    return result


def validate_outputs(outputs: dict) -> None:
    if not isinstance(outputs, dict) or set(outputs) != OUTPUT_KEYS:
        raise ShadowError("Planning job outputs are incomplete")
    if outputs["full_shadow"] not in {"true", "false"}:
        raise ShadowError("Planning execution mode is invalid")
    oid(outputs["execution_commit"])
    oid(outputs["execution_tree"])
    for key in ("policy_sha256", "bindings_sha256", "plan_digest"):
        if not isinstance(outputs[key], str) or re.fullmatch(r"[0-9a-f]{64}", outputs[key]) is None:
            raise ShadowError("Planning identity output is invalid")


def verify_jobs(
    needs: dict, *, full_shadow: bool, execution_commit: str, execution_tree: str
) -> dict:
    """Check this fixed workflow DAG, never manufacture generic per-cell receipts."""
    oid(execution_commit)
    oid(execution_tree)
    if not isinstance(needs, dict) or set(needs) != {"planning", *JOBS}:
        raise ShadowError("Shadow aggregate dependency inventory is incomplete")
    if any(not isinstance(value, dict) for value in needs.values()):
        raise ShadowError("Shadow dependency result is invalid")
    if needs["planning"].get("result") != "success":
        raise ShadowError("Shadow planning did not succeed")
    outputs = needs["planning"].get("outputs")
    validate_outputs(outputs)
    if (
        outputs["full_shadow"] != ("true" if full_shadow else "false")
        or outputs["execution_commit"] != execution_commit
        or outputs["execution_tree"] != execution_tree
    ):
        raise ShadowError("Shadow source or execution mode differs from the caller")
    expected = "success" if full_shadow else "skipped"
    if any(needs[name].get("result") != expected for name in JOBS):
        raise ShadowError("A shadow workflow did not meet its required outcome")
    return {
        "test-shadow-version": 1,
        "status": "workflows-completed" if full_shadow else "comparison-completed",
        "activation": "disabled",
        "identity": outputs,
        "jobs": {name: needs[name]["result"] for name in JOBS},
        "evidence": "Original workflow artifacts remain authoritative; no cell receipts inferred",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    for name in ("repo", "policy", "baseline-policy", "output"):
        plan.add_argument("--" + name, type=Path, required=True)
    for name in ("range", "execution-commit", "execution-tree", "adapter-commit", "adapter-tree"):
        plan.add_argument("--" + name, required=True)
    plan.add_argument("--full-shadow", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("--execution-commit", required=True)
    verify.add_argument("--execution-tree", required=True)
    verify.add_argument("--full-shadow", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            needs = decode(os.environ.get("SHADOW_NEEDS", "").encode(), 16384)
            print(
                encode(
                    verify_jobs(
                        needs,
                        full_shadow=args.full_shadow,
                        execution_commit=args.execution_commit,
                        execution_tree=args.execution_tree,
                    )
                ).decode()
            )
        else:
            documents = generate(
                args.repo,
                policy_path=args.policy,
                baseline_path=args.baseline_policy,
                revision_range=args.range,
                execution_commit=args.execution_commit,
                execution_tree=args.execution_tree,
                adapter_commit=args.adapter_commit,
                adapter_tree=args.adapter_tree,
                full_shadow=args.full_shadow,
            )
            args.output.mkdir(parents=True, exist_ok=False)
            for filename, document in documents.items():
                (args.output / filename).write_bytes(encode(document))
            outputs = job_outputs(documents, full_shadow=args.full_shadow)
            if os.environ.get("GITHUB_OUTPUT"):
                with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
                    stream.write(
                        "".join(f"{key}={value}\n" for key, value in sorted(outputs.items()))
                    )
            print(encode(documents["comparison.json"]).decode())
    except (RuntimeError, ValueError, TypeError, KeyError, OSError, ImportError, RecursionError):
        print(
            encode(
                {
                    "test-shadow-version": 1,
                    "status": "error",
                    "activation": "disabled",
                    "error": "Shadow planning or aggregation failed; inspect bounded artifacts and inputs",
                }
            ).decode()
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
