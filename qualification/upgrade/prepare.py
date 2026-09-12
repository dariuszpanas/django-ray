"""Export the reviewed released source; never download a floating old package."""

from __future__ import annotations

import argparse
import subprocess
import tarfile
from pathlib import Path

from qualification.upgrade.contract import BASELINE_COMMIT


def verify_archive(path: Path) -> None:
    """Git archive carries its exact commit in the PAX header."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("expected-bounded-baseline-git-archive")
    with tarfile.open(path) as archive:
        if archive.pax_headers.get("comment") != BASELINE_COMMIT:
            raise ValueError("wrong-baseline-archive-commit")
        members = archive.getmembers()
        if len(members) > 10000 or any(
            member.name.startswith("/")
            or ".." in Path(member.name).parts
            or not (member.isfile() or member.isdir())
            for member in members
        ):
            raise ValueError("unsupported-baseline-archive-entry")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="new directory outside the checkout")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    destination = args.destination.resolve()
    if destination.is_relative_to(root) or destination.exists():
        raise SystemExit("expected-new-baseline-directory-outside-checkout")
    observed = subprocess.check_output(
        ["git", "rev-parse", "v0.4.0^{commit}"],
        cwd=root,
        timeout=10,
        text=True,
    ).strip()
    if observed != BASELINE_COMMIT:
        raise SystemExit("v0.4.0-does-not-match-reviewed-baseline")
    destination.mkdir(mode=0o755)
    path = destination / "source.tar"
    with path.open("xb") as stream:
        subprocess.run(
            ["git", "archive", "--format=tar", BASELINE_COMMIT],
            cwd=root,
            stdout=stream,
            check=True,
            timeout=30,
        )
    verify_archive(path)
    print(f"baseline={BASELINE_COMMIT} archive={path}")


if __name__ == "__main__":
    main()
