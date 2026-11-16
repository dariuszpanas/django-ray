"""Record bounded direct execution facts; never authenticate or reuse them.

Only fixed context fields and file identities leave this producer. A future
consumer must authenticate the GitHub run, attempt, workflow and artifact bytes
independently. Mutable build inputs are not a reproducibility claim.
Workflow path/SHA are the caller's GitHub context under workflow_call, not
independent attestation of the called workflow. Runner image fields are optional
environment observations, not authenticated platform or build-material identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA = "django-ray.application-execution-witness"
MAX_EVENT_BYTES = 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_FILE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024
STEPS = ("checkout", "profile", "ci", "tools", "build", "cluster", "native", "cleanup", "receipts")
OUTCOMES = frozenset({"success", "failure", "cancelled", "skipped", "unavailable"})
FAILURES = frozenset(
    {
        "context_invalid",
        "profile_invalid",
        "runner_invalid",
        "step_outcomes_invalid",
        "steps_not_successful",
        "source_unavailable",
        "images_unavailable",
        "summary_unavailable",
        "artifact_unavailable",
        "receipt_inventory_incomplete",
    }
)
RECEIPT_NAMES = (
    "setup",
    "reporting",
    "producers",
    "summary",
    *(
        f"{generation}-{layer}"
        for generation in ("before", "after")
        for layer in (
            "first",
            "nodes",
            "core",
            "workflows",
            "rq2",
            "showcase",
            "protocol",
            "protected-logs",
        )
    ),
)
FILES = tuple(
    sorted(
        [f"application-evidence/{name}.json" for name in RECEIPT_NAMES]
        + [
            f"application-build/{name}"
            for name in (
                "source.txt",
                "source.sha256",
                "images.json",
                "validation-intent.txt",
                "resource-profile.txt",
            )
        ]
    )
)


class WitnessError(ValueError):
    """An invalid bounded input; callers publish only their fixed category."""


def require(condition: object) -> None:
    if not condition:
        raise WitnessError("Invalid execution witness input")


def identity(value: Any) -> int:
    if type(value) is str:
        require(re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None)
        value = int(value)
    require(type(value) is int and 0 < value <= 2**63 - 1)
    return value


def hexadecimal(value: Any, length: int = 40) -> str:
    require(type(value) is str and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None)
    return value


def bounded_file(path: Path, maximum: int) -> bytes:
    try:
        require(not any(item.is_symlink() for item in (path, *path.parents)))
        require(stat.S_ISREG(path.stat().st_mode))
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
        require(0 < len(raw) <= maximum)
        return raw
    except OSError:
        raise WitnessError("Invalid execution witness input") from None


def decode(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result

    def constant(_value):
        raise WitnessError("Invalid execution witness input")

    def number(value):
        parsed = float(value)
        require(math.isfinite(parsed))
        return parsed

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except (ValueError, RecursionError):
        raise WitnessError("Invalid execution witness input") from None


def context(env: Mapping[str, Any], event: Any) -> dict:
    require(type(event) is dict)
    repository = env.get("GITHUB_REPOSITORY")
    if type(repository) is not str:
        raise WitnessError("Invalid execution witness input")
    require(
        len(repository) <= 200
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is not None
    )
    repo = event.get("repository")
    require(type(repo) is dict and repo.get("full_name") == repository)
    repo_id = identity(env.get("GITHUB_REPOSITORY_ID"))
    require(identity(repo.get("id")) == repo_id)
    event_name = env.get("GITHUB_EVENT_NAME")
    require(event_name in {"pull_request", "workflow_dispatch"})
    workflow = env.get("GITHUB_WORKFLOW_REF", "")
    prefix = repository + "/.github/workflows/"
    require(type(workflow) is str and len(workflow) <= 512 and workflow.startswith(prefix))
    match = re.fullmatch(
        r"(application-qualification|qualification-gate|test-planning-shadow)\.yml@"
        r"(refs/pull/[1-9][0-9]{0,18}/merge|refs/(?:heads|tags)/[A-Za-z0-9_./+-]{1,255})",
        workflow[len(prefix) :],
    )
    if match is None:
        raise WitnessError("Invalid execution witness input")
    source_head = hexadecimal(env.get("SOURCE_SHA"))
    actor = identity(env.get("GITHUB_ACTOR_ID"))
    require(type(event.get("sender")) is dict and identity(event["sender"].get("id")) == actor)
    pr = None
    if event_name == "pull_request":
        pull = event.get("pull_request")
        require(type(pull) is dict)
        try:
            pr = {
                "number": identity(pull["number"]),
                "author_id": identity(pull["user"]["id"]),
                "base_repository_id": identity(pull["base"]["repo"]["id"]),
                "head_repository_id": identity(pull["head"]["repo"]["id"]),
                "base_sha": hexadecimal(pull["base"]["sha"]),
                "head_sha": hexadecimal(pull["head"]["sha"]),
            }
        except (KeyError, TypeError):
            raise WitnessError("Invalid execution witness input") from None
        require(pr["base_repository_id"] == repo_id and pr["head_sha"] == source_head)
        require(match[2] == f"refs/pull/{pr['number']}/merge")
    else:
        require(hexadecimal(env.get("GITHUB_SHA")) == source_head)
        require(match[2].startswith(("refs/heads/", "refs/tags/")))
    return {
        "repository": repository,
        "repository_id": repo_id,
        "run_id": identity(env.get("GITHUB_RUN_ID")),
        "attempt": identity(env.get("GITHUB_RUN_ATTEMPT")),
        "actor_id": actor,
        "event": event_name,
        "event_sha": hexadecimal(env.get("GITHUB_SHA")),
        "workflow_sha": hexadecimal(env.get("GITHUB_WORKFLOW_SHA")),
        "workflow_path": ".github/workflows/" + match[1] + ".yml",
        "source_head": source_head,
        "pull_request": pr,
    }


def profile(env: Mapping[str, Any]) -> dict:
    name, intent = env.get("RESOURCE_PROFILE"), env.get("VALIDATION_INTENT")
    require(intent in {"acceptance", "diagnostic"})
    require(name in {"standard", "constrained-ray"})
    require(name == "standard" or intent == "diagnostic")
    return {"resource_profile": name, "validation_intent": intent}


def runner(env: Mapping[str, Any]) -> dict:
    require(env.get("RUNNER_OS") == "Linux" and env.get("RUNNER_ARCH") == "X64")
    require(env.get("RUNNER_ENVIRONMENT") == "github-hosted")
    observed = {}
    for key in ("ImageOS", "ImageVersion"):
        value = env.get(key)
        require(
            value is None
            or (type(value) is str and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is not None)
        )
        observed[key] = value
    return {"os": "Linux", "arch": "X64", "environment": "github-hosted", **observed}


def source(files: dict[str, bytes], github: dict, selected: dict) -> dict:
    lines = files["application-build/source.txt"].decode("ascii").splitlines()
    require(len(lines) == 2 and hexadecimal(lines[0]) == github["source_head"])
    tree = hexadecimal(lines[1])
    archive = files["application-build/source.sha256"].decode("ascii").splitlines()
    require(len(archive) == 1)
    match = re.fullmatch(r"([0-9a-f]{64})  [^\r\n]{1,512}", archive[0])
    if match is None:
        raise WitnessError("Invalid execution witness input")
    for filename, key in (
        ("resource-profile.txt", "resource_profile"),
        ("validation-intent.txt", "validation_intent"),
    ):
        require(files["application-build/" + filename].decode("ascii").strip() == selected[key])
    return {"head": lines[0], "tree": tree, "archive_sha256": match[1]}


def images(files: dict[str, bytes]) -> dict:
    inspected = decode(files["application-build/images.json"])
    require(type(inspected) is list and len(inspected) == 2)
    references = []
    for record, role in zip(inspected, ("base", "core"), strict=True):
        require(type(record) is dict)
        refs = record.get("RepoDigests")
        require(type(refs) is list and len(refs) == 1)
        require(
            type(refs[0]) is str
            and re.fullmatch(rf"localhost:5001/application-{role}@sha256:[0-9a-f]{{64}}", refs[0])
            is not None
        )
        references.append(refs[0])
    return {"base": references[0], "application": references[1]}


def summary(files: dict[str, bytes], exported: dict, selected: dict, tested: dict) -> dict:
    value = decode(files["application-evidence/summary.json"])
    require(type(value) is dict)
    require(value["status"] == "passed" and value["namespace_removed"] is True)
    require(value["source_tree"] == exported["tree"] and value["image"] == tested["application"])
    require(value["cold_ray"] == "required" and value["complete_application_gate"] is False)
    qualified = value["qualification_profile"]
    require(type(qualified) is dict)
    require(all(qualified[key] == item for key, item in selected.items()))
    return {
        "status": "passed",
        "namespace_removed": True,
        "cold_ray": "required",
        "source_tree_matched": True,
        "qualification_profile_sha256": hashlib.sha256(encode(qualified)).hexdigest(),
    }


def encode(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def produce(env: Mapping[str, Any], *, event_path: Path, temporary: Path) -> dict:
    """Retain partial fixed observations when earlier direct execution failed."""
    failed = []

    def observe(code, operation):
        require(code in FAILURES)
        try:
            return operation()
        except (ValueError, KeyError, TypeError):
            failed.append(code)
            return None

    github = observe(
        "context_invalid", lambda: context(env, decode(bounded_file(event_path, MAX_EVENT_BYTES)))
    )
    selected = observe("profile_invalid", lambda: profile(env))
    observed_runner = observe("runner_invalid", lambda: runner(env))
    outcomes = {step: env.get("WITNESS_STEP_" + step.upper(), "unavailable") for step in STEPS}
    if any(type(value) is not str or value not in OUTCOMES for value in outcomes.values()):
        failed.append("step_outcomes_invalid")
        outcomes = dict.fromkeys(STEPS, "unavailable")
    expected = dict.fromkeys(STEPS, "success")
    if selected is not None and selected["validation_intent"] == "diagnostic":
        expected["ci"] = "skipped"
    if outcomes != expected:
        failed.append("steps_not_successful")
    files = {}
    total = 0
    for name in FILES:
        raw = observe(
            "receipt_inventory_incomplete",
            lambda name=name, total=total: bounded_file(
                temporary / name, min(MAX_FILE_BYTES, MAX_TOTAL_FILE_BYTES - total)
            ),
        )
        if raw is not None:
            total += len(raw)
            files[name] = raw
    exported = observe("source_unavailable", lambda: source(files, github, selected))
    tested = observe("images_unavailable", lambda: images(files))
    observed = observe("summary_unavailable", lambda: summary(files, exported, selected, tested))

    def artifact():
        require(github is not None and outcomes["receipts"] == "success")
        return {
            "id": identity(env.get("WITNESS_ARTIFACT_ID")),
            "digest": "sha256:" + hexadecimal(env.get("WITNESS_ARTIFACT_DIGEST"), 64),
            "name": f"application-core-{github['run_id']}",
        }

    uploaded = observe("artifact_unavailable", artifact)
    result = {
        "schema": SCHEMA,
        "schema_version": 1,
        "mode": "direct_execution",
        "status": "incomplete" if failed else "passed",
        "failures": sorted(set(failed)),
        "github": github,
        "profile": selected,
        "runner": observed_runner,
        "steps": outcomes,
        "source": exported,
        "tested_images": tested,
        "observation": observed,
        "receipt_artifact": uploaded,
        "receipt_files": [
            {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in sorted(files.items())
        ],
        "self_authenticating": False,
        "reuse_enabled": False,
        "complete_application_gate": False,
    }
    require(len(encode(result)) <= MAX_OUTPUT_BYTES)
    return result


def main() -> int:
    try:
        root = Path(__file__).resolve().parents[2]
        temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
        require(temporary.is_absolute() and not temporary.is_relative_to(root))
        result = produce(
            os.environ, event_path=Path(os.environ["GITHUB_EVENT_PATH"]), temporary=temporary
        )
        output = temporary / "application-witness"
        output.mkdir(exist_ok=False)
        (output / "execution-witness.json").write_bytes(encode(result))
        print("Application execution witness: " + result["status"])
        return 0 if result["status"] == "passed" else 1
    except (OSError, WitnessError, KeyError, TypeError, ValueError, RecursionError):
        print("Application execution witness unavailable (invalid bounded input or output).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
