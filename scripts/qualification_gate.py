"""Require applicable qualification workflows for one immutable pull request head."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


class GateError(RuntimeError):
    """Fixed diagnostic without API response or application payload data."""


def matches(path: str, pattern: str) -> bool:
    """The policy deliberately supports only exact paths and directory suffixes."""
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    if any(char in pattern for char in "*?[]!"):
        raise GateError("Unsupported qualification path pattern")
    return path == pattern


def select(policy, files):
    if not isinstance(policy, list) or not policy:
        raise GateError("Missing qualification policy")
    if len({entry["id"] for entry in policy}) != len(policy):
        raise GateError("Duplicate qualification identity")
    selected = []
    for entry in policy:
        if set(entry) != {"id", "workflow", "paths"}:
            raise GateError("Invalid qualification policy fields")
        if not re.fullmatch(r"[a-z][a-z0-9-]*\.yml", entry["workflow"]):
            raise GateError("Invalid qualification workflow")
        if entry["id"] not in {"application", "native", "latency"}:
            raise GateError("Invalid qualification identity")
        if not isinstance(entry["paths"], list) or not entry["paths"]:
            raise GateError("Missing qualification paths")
        for pattern in entry["paths"]:
            matches("", pattern)
        if any(matches(path, pattern) for path in files for pattern in entry["paths"]):
            selected.append(entry)
    return selected


def changed_files(api, repo, number, sha):
    pr = api(f"repos/{repo}/pulls/{number}")
    if pr.get("head", {}).get("sha") != sha or pr.get("state") != "open":
        raise GateError("Pull request head changed or is no longer open")
    count = pr.get("changed_files")
    if type(count) is not int or not 0 <= count <= 3000:
        raise GateError("Changed file inventory exceeds the gate limit")
    result = []
    for page in range(1, (count + 99) // 100 + 1):
        batch = api(f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise GateError("Invalid changed file inventory")
        result.extend(batch)
    if len(result) != count:
        raise GateError("Incomplete changed file inventory")
    names = set()
    for item in result:
        names.add(item["filename"])
        if item.get("previous_filename"):
            names.add(item["previous_filename"])
    return names


def verify(results):
    expected = {"selection", "application", "native", "latency"}
    if set(results) != expected or results["selection"].get("result") != "success":
        raise GateError("Qualification selection did not pass")
    outputs = results["selection"].get("outputs", {})
    if set(outputs) != expected - {"selection"}:
        raise GateError("Qualification selection outputs are incomplete")
    for name in expected - {"selection"}:
        requested = outputs[name]
        if requested not in {"true", "false"}:
            raise GateError("Qualification selection output is invalid")
        wanted = "success" if requested == "true" else "skipped"
        if results[name].get("result") != wanted:
            raise GateError(f"Qualification did not meet its selected outcome: {name}")
    return {
        name: "passed" if outputs[name] == "true" else "not_applicable"
        for name in sorted(expected - {"selection"})
    }


def main():
    def api(endpoint):
        try:
            result = subprocess.run(
                ["gh", "api", endpoint], capture_output=True, text=True, check=True, timeout=20
            )
            return json.loads(result.stdout)
        except (subprocess.SubprocessError, OSError, ValueError):
            raise GateError("Unable to read qualification metadata") from None

    try:
        if os.environ.get("GATE_MODE") == "verify":
            result = verify(json.loads(os.environ["QUALIFICATION_RESULTS"]))
        else:
            repo, sha = os.environ["GITHUB_REPOSITORY"], os.environ["SOURCE_SHA"]
            number = int(os.environ["PR_NUMBER"])
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                raise GateError("Invalid repository")
            if number <= 0 or not re.fullmatch("[0-9a-f]{40}", sha):
                raise GateError("Invalid pull request identity")
            policy = json.loads(Path(__file__).with_name("qualification_policy.json").read_text())
            files = changed_files(api, repo, number, sha)
            required = select(policy, files)
            if changed_files(api, repo, number, sha) != files:
                raise GateError("Changed file selection changed")
            result = {entry["id"]: "true" if entry in required else "false" for entry in policy}
            if set(result) != {"application", "native", "latency"}:
                raise GateError("Qualification policy is incomplete")
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                for key, value in result.items():
                    output.write(f"{key}={value}\n")
    except GateError as error:
        print(f"::error::{error}")
        return 1
    except (ValueError, KeyError, TypeError, OSError):
        print("::error::Qualification gate input is invalid")
        return 1
    text = json.dumps(result, sort_keys=True)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
            output.write("```json\n" + text + "\n```\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
