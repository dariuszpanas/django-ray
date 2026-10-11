"""Attempt every owned infrastructure cleanup before reporting a failure."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path


def command(argv: list[str], *, seconds: float) -> str:
    if seconds <= 0:
        raise TimeoutError("Cleanup deadline expired")
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            argv, stdout=output, stderr=subprocess.DEVNULL, timeout=seconds, check=False
        )
        if result.returncode or output.tell() > 16384:
            raise RuntimeError("Cleanup command failed")
        output.seek(0)
        return output.read(16385).decode()


def cleanup(run_id: str) -> dict:
    if re.fullmatch(r"[0-9]{1,20}", run_id) is None:
        raise ValueError("Invalid owned run identifier")
    namespace = "monitoring-" + run_id
    deadline = time.monotonic() + 280
    receipt: dict = {"status": "passed", "failed_components": []}
    objects = (
        (
            "disposable_cluster",
            "monitoring-qualification-control-plane",
            "io.x-k8s.kind.cluster",
            "monitoring-qualification",
            ["kind", "delete", "cluster", "--name", "monitoring-qualification"],
            180,
        ),
        (
            "registry",
            "monitoring-registry",
            "qualification.django-ray/namespace",
            namespace,
            ["docker", "rm", "-f", "monitoring-registry"],
            20,
        ),
        (
            "browser",
            namespace + "-browser",
            "qualification.django-ray/namespace",
            namespace,
            ["docker", "rm", "-f", namespace + "-browser"],
            20,
        ),
    )
    for component, name, label, expected, remove, timeout in objects:
        failed = False
        present = ["docker", "ps", "-aq", "--filter", "name=^" + name + "$"]
        try:
            if command(present, seconds=min(5, deadline - time.monotonic())).strip():
                owner = command(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        '{{index .Config.Labels "' + label + '"}}',
                        name,
                    ],
                    seconds=min(5, deadline - time.monotonic()),
                ).strip()
                if owner != expected:
                    raise ValueError("Owned cleanup label mismatch")
                command(remove, seconds=min(timeout, deadline - time.monotonic()))
        except Exception:
            failed = True
        # A failed removal or label check must not bypass absence verification,
        # and no failure for this object may suppress the next object's attempt.
        try:
            removed = not command(present, seconds=min(5, deadline - time.monotonic())).strip()
        except Exception:
            removed = False
            failed = True
        receipt[component + "_removed"] = removed
        if failed or not removed:
            receipt["failed_components"].append(component)
            receipt["status"] = "failed"
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    receipt = cleanup(os.environ["GITHUB_RUN_ID"])
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "infrastructure-cleanup.json").write_text(json.dumps(receipt) + "\n")
    raise SystemExit(0 if receipt["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
