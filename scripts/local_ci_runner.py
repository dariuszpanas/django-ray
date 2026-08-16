"""Run the private CI Make target without inherited GNU Make recursion state."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

from scripts.local_resource_coordinator import (
    LocalResourceCoordinationError,
    require_inherited_local_resources,
)

_MAKE_ENVIRONMENT_KEYS: Final = frozenset(
    {
        "GNUMAKEFLAGS",
        "MAKE",
        "MAKE_COMMAND",
        "MAKEFLAGS",
        "MAKEFILES",
        "MAKELEVEL",
        "MAKEOVERRIDES",
        "MAKE_RESTARTS",
        "MAKE_TERMERR",
        "MAKE_TERMOUT",
        "MFLAGS",
    }
)
_GIT_CONFIG_ENVIRONMENT_KEYS: Final = frozenset(
    {
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
    }
)
_GIT_CONFIG_ENVIRONMENT_PREFIXES: Final = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
)
_DISABLED_FSMONITOR_ENVIRONMENT: Final = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.fsmonitor",
    "GIT_CONFIG_VALUE_0": "false",
}


def _is_scrubbed_environment_key(key: str) -> bool:
    normalized = key.upper()
    return (
        normalized in _MAKE_ENVIRONMENT_KEYS
        or normalized in _GIT_CONFIG_ENVIRONMENT_KEYS
        or normalized.startswith(_GIT_CONFIG_ENVIRONMENT_PREFIXES)
    )


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


def main(argv: list[str] | None = None) -> int:
    """Invoke exactly one serial private target from the repository root."""

    _parser().parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve() != root:
        print(
            "FAILED [local-resources]: CI runner must start at the repository root", file=sys.stderr
        )
        return 2
    try:
        require_inherited_local_resources(profile="ci-final", rootpath=root)
    except LocalResourceCoordinationError:
        print("FAILED [local-resources]: valid inherited CI ownership is required", file=sys.stderr)
        return 4
    environment = {
        key: value for key, value in os.environ.items() if not _is_scrubbed_environment_key(key)
    }
    environment.update(_DISABLED_FSMONITOR_ENVIRONMENT)
    try:
        result = subprocess.run(
            ["make", "--no-print-directory", "-j1", "_ci-owned"],
            cwd=root,
            env=environment,
            check=False,
            shell=False,
        )
    except OSError:
        print("FAILED [local-resources]: private CI Make launch failed", file=sys.stderr)
        return 2
    return result.returncode if result.returncode >= 0 else 128 + abs(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
