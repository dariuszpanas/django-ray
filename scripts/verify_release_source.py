"""Fail closed unless a release workflow is using the intended Git source."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_RELEASE_TAG_RE = re.compile(r"v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")


def _normalize_full_sha(value: str, *, field: str) -> str:
    normalized = value.strip()
    if _FULL_SHA_RE.fullmatch(normalized) is None:
        raise ValueError(f"{field} must be a lowercase full 40-character commit SHA")
    return normalized


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise ValueError(f"Git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def verify_manual_candidate_source(
    root: Path,
    *,
    candidate_sha: str,
    event_sha: str,
) -> str:
    """Require the manual input, event, checkout, and fetched main to agree."""
    identities = {
        "candidate SHA": _normalize_full_sha(candidate_sha, field="candidate SHA"),
        "GitHub event SHA": _normalize_full_sha(event_sha, field="GitHub event SHA"),
        "checked-out HEAD": _normalize_full_sha(
            _git(root, "rev-parse", "HEAD"),
            field="checked-out HEAD",
        ),
        "fetched origin/main": _normalize_full_sha(
            _git(root, "rev-parse", "refs/remotes/origin/main^{commit}"),
            field="fetched origin/main",
        ),
    }
    if len(set(identities.values())) != 1:
        details = ", ".join(f"{name}={value}" for name, value in identities.items())
        raise ValueError(f"manual release source identities do not agree: {details}")
    return identities["candidate SHA"]


def verify_production_tag_source(
    root: Path,
    *,
    tag: str,
    event_sha: str,
) -> str:
    """Require an annotated release tag, event SHA, and checkout to agree."""
    normalized_tag = tag.strip()
    if _RELEASE_TAG_RE.fullmatch(normalized_tag) is None:
        raise ValueError(f"production release tag is invalid: {tag!r}")
    tag_ref = f"refs/tags/{normalized_tag}"
    if _git(root, "cat-file", "-t", tag_ref) != "tag":
        raise ValueError(f"production release tag {normalized_tag} must be annotated")

    identities = {
        "GitHub event SHA": _normalize_full_sha(event_sha, field="GitHub event SHA"),
        "checked-out HEAD": _normalize_full_sha(
            _git(root, "rev-parse", "HEAD"),
            field="checked-out HEAD",
        ),
        "annotated tag target": _normalize_full_sha(
            _git(root, "rev-parse", f"{tag_ref}^{{commit}}"),
            field="annotated tag target",
        ),
        "fetched origin/main": _normalize_full_sha(
            _git(root, "rev-parse", "refs/remotes/origin/main^{commit}"),
            field="fetched origin/main",
        ),
    }
    if len(set(identities.values())) != 1:
        details = ", ".join(f"{name}={value}" for name, value in identities.items())
        raise ValueError(f"production release source identities do not agree: {details}")
    return normalized_tag


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manual-candidate", metavar="FULL_SHA")
    source.add_argument("--production-tag", metavar="TAG")
    parser.add_argument("--event-sha", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    try:
        if args.manual_candidate is not None:
            verified = verify_manual_candidate_source(
                args.root,
                candidate_sha=args.manual_candidate,
                event_sha=args.event_sha,
            )
            print(f"manual candidate source verified: {verified}")
        else:
            verified = verify_production_tag_source(
                args.root,
                tag=args.production_tag,
                event_sha=args.event_sha,
            )
            print(f"production tag source verified: {verified}")
    except (OSError, ValueError) as exc:
        print(f"Release source validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
