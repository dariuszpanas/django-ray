"""The exit observer must preserve failures, including native child signals."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.observe_pytest_exit import observe


@pytest.mark.parametrize("passes, expected", [(True, 0), (False, 1)])
def test_real_pytest_return_and_shutdown_are_observed(tmp_path, passes, expected):
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(f"def test_sample():\n    assert {passes}\n")
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTEST_ADDOPTS", None)
    script = Path(__file__).resolve().parents[2] / "scripts" / "observe_pytest_exit.py"
    result = subprocess.run(
        [sys.executable, str(script), "--", "-q", str(test_file)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert [record["pytest_exit_observer"] for record in records] == [
        "child_started",
        "pytest_returned",
        "atexit_checkpoint",
        "process_exit",
    ]
    assert records[1]["returncode"] == expected
    assert records[-1]["returncode"] == expected


@pytest.mark.parametrize("code", [0, 1, 5, 139])
def test_observe_preserves_child_exit(code, capsys):
    assert observe([sys.executable, "-c", f"raise SystemExit({code})"]) == code
    assert json.loads(capsys.readouterr().out) == {
        "pytest_exit_observer": "process_exit",
        "returncode": code,
        "signal": 0,
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX child signal return codes")
def test_observe_reports_signal_without_converting_to_success(capsys):
    code = observe(
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"]
    )
    assert code == 128 + signal.SIGTERM
    record = json.loads(capsys.readouterr().out)
    assert record["returncode"] == -signal.SIGTERM
    assert record["signal"] == signal.SIGTERM
