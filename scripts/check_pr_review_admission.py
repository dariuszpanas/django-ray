"""Admit automatic review publication only for the repository owner's own branch.

Run from the trusted default-branch checkout. GitHub API metadata is data only;
pull-request code, titles, bodies and comments are never executed or printed.
This is publisher admission, not an assessment of a contributor's intentions.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OWNER_USER_ID = 15094983
MAX_RESPONSE_BYTES = 1024 * 1024
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
HEAD_PATTERN = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str
    head_sha: str = ""


def _object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _identity(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def evaluate(
    pull_request: object, *, repository: str, repository_id: int, number: int
) -> Admission:
    """Fail closed without treating names, prior reviews or associations as trust."""
    pr = _object(pull_request)
    base = _object(pr.get("base"))
    head = _object(pr.get("head"))
    base_repo = _object(base.get("repo"))
    head_repo = _object(head.get("repo"))
    owner = _object(base_repo.get("owner"))
    author = _object(pr.get("user"))
    head_sha = head.get("sha")
    if not isinstance(head_sha, str) or HEAD_PATTERN.fullmatch(head_sha) is None:
        return Admission(False, "unverified-metadata")
    if (
        not _identity(pr.get("number"), number)
        or not _identity(base_repo.get("id"), repository_id)
        or base_repo.get("full_name") != repository
        or not _identity(owner.get("id"), OWNER_USER_ID)
        or owner.get("type") != "User"
        or not isinstance(base_repo.get("default_branch"), str)
        or not base_repo["default_branch"]
        or base.get("ref") != base_repo["default_branch"]
    ):
        return Admission(False, "unverified-metadata", head_sha)
    if pr.get("state") != "open" or pr.get("draft") is not False:
        return Admission(False, "inactive-pull-request", head_sha)
    if not _identity(author.get("id"), OWNER_USER_ID) or author.get("type") != "User":
        return Admission(False, "external-author", head_sha)
    if (
        not _identity(head_repo.get("id"), repository_id)
        or head_repo.get("full_name") != repository
    ):
        return Admission(False, "external-source-branch", head_sha)
    return Admission(True, "owner-same-repository", head_sha)


def fetch_pull_request(repository: str, number: int) -> object:
    """Fetch current metadata on every publisher path with a bounded API request."""
    result = subprocess.run(
        ["gh", "api", "--method", "GET", f"repos/{repository}/pulls/{number}"],
        check=True,
        capture_output=True,
        timeout=20,
    )
    if len(result.stdout) > MAX_RESPONSE_BYTES:
        raise ValueError("Pull-request metadata exceeds its bound")
    return json.loads(result.stdout)


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    repository_id = os.environ.get("GITHUB_REPOSITORY_ID", "")
    number = os.environ.get("PR_NUMBER", "")
    try:
        if (
            REPOSITORY_PATTERN.fullmatch(repository) is None
            or re.fullmatch(r"[1-9][0-9]{0,18}", repository_id) is None
            or re.fullmatch(r"[1-9][0-9]{0,18}", number) is None
        ):
            raise ValueError("Invalid trusted workflow context")
        admission = evaluate(
            fetch_pull_request(repository, int(number)),
            repository=repository,
            repository_id=int(repository_id),
            number=int(number),
        )
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write(f"allowed={str(admission.allowed).lower()}\n")
            stream.write(f"reason={admission.reason}\n")
            stream.write(f"head_sha={admission.head_sha}\n")
        decision = (
            "Owner-authored same-repository pull request admitted to automated review."
            if admission.allowed
            else "Automatic review withheld. This pull request needs maintainer review; "
            "the publisher will not submit an approval or a change request."
        )
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(f"{decision}\n\nPR #{number}; admission: `{admission.reason}`.\n")
        print(f"Review publisher admission: {admission.reason}")
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        # API errors can contain response bodies. Keep all unverified data and
        # credentials out of logs; a failed step prevents publication.
        print("Automatic review withheld: current metadata could not be verified.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
