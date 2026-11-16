"""Run the resource-free browser contract for live Admin task polling."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_live_admin_polling_javascript_contract() -> None:
    """Transient HTTP failures retry without weakening authentication stops."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("Node.js is required for the live Admin polling contract in CI")
        pytest.skip("Node.js is unavailable for the live Admin polling contract")

    result = subprocess.run(
        [node, "--test", "tests/javascript/task_live.test.mjs"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
