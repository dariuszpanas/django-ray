"""Actual Linux descendant/deadline checks; no Ray, database or external service."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.runtime_env_growth.evidence import OwnedRoot, scan_tree

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux process/root ownership proof"
)


def test_owned_root_cleanup_preserves_sibling_and_never_follows_link(tmp_path):
    sibling = tmp_path / "foreign"
    sibling.mkdir()
    (sibling / "keep").write_text("preserved")
    root = tmp_path / "owned"
    root.mkdir()
    (root / "link").symlink_to(sibling, target_is_directory=True)
    inventory = scan_tree(root)
    assert inventory["entries"] == 1
    assert inventory["roles"]["other"]["symlinks"] == 1
    assert OwnedRoot.capture(root).remove(quiescent=True)
    assert not root.exists()
    assert (sibling / "keep").read_text() == "preserved"


@pytest.mark.parametrize("phase", ["startup", "shutdown"])
def test_external_deadline_stops_blocked_stage_and_detached_descendant(tmp_path, phase):
    # A separate supervisor process prevents cleanup from owning pytest's children.
    helper = tmp_path / "supervisor.py"
    driver = tmp_path / "driver.py"
    driver.write_text("""import json, os, signal, subprocess, sys, time
fd = int(os.environ["DR_GROWTH_FD"])
os.set_inheritable(fd, False)
out = os.fdopen(fd, "w", buffering=1)
sys.stdin.readline()
subprocess.Popen([sys.executable, "-c", "import os,signal,time; os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"], close_fds=True)
print(json.dumps({"kind":"phase","session":1,"index":0,"phase":sys.argv[1]}), file=out)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(60)
""")
    helper.write_text("""import os, sys, time
from pathlib import Path
from scripts.runtime_env_growth import supervisor as s
s.enable_subreaper()
s.PHASES = [sys.argv[2]]
s.PHASE_SECONDS = {sys.argv[2]: 0.25, "startup": 0.25}
s.boundary_snapshot = lambda: {}
result = s.supervise([sys.executable, sys.argv[1], sys.argv[2]], environment=dict(os.environ), total_deadline=time.monotonic()+8, before={}, root=Path(sys.argv[1]).parent)
assert result["failure"] == "deadline", result
assert result["owned_processes_stopped"] is True, result
assert result["forced_cleanup_count"] >= 2, result
assert result["completed_sessions"] == 0
assert result["passed"] is False
""")
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    result = subprocess.run(
        [sys.executable, str(helper), str(driver), phase],
        env=environment,
        capture_output=True,
        timeout=12,
    )
    assert result.returncode == 0, result.stderr.decode()[-2000:]


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("timeout", "command_timeout"),
        ("overflow", "output_limit"),
        ("leader_exit", "command_descendants"),
    ],
)
def test_host_cli_owner_stops_detached_stdout_holder(tmp_path, mode, reason):
    driver = tmp_path / "cli.py"
    driver.write_text("""import os, signal, subprocess, sys, time
child = "import os,signal,time; os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
subprocess.Popen([sys.executable, "-c", child])
if sys.argv[1] == "leader_exit":
    raise SystemExit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if sys.argv[1] == "overflow":
    os.write(1, b"x" * 8192)
time.sleep(60)
""")
    helper = tmp_path / "owner.py"
    helper.write_text("""import os, sys
import psutil
from scripts.benchmark_runtime_env_growth import bounded_command, CommandError
try:
    bounded_command([sys.executable, sys.argv[1], sys.argv[2]], 0.4 if sys.argv[2] == "timeout" else 4, limit=64)
except CommandError as exc:
    assert str(exc) == sys.argv[3], str(exc)
    assert exc.cleanup_complete
else:
    raise AssertionError("unexpected success")
assert not psutil.Process().children(recursive=True)
try:
    os.waitpid(-1, os.WNOHANG)
except ChildProcessError:
    pass
else:
    raise AssertionError("owned descendants remain")
""")
    result = subprocess.run(
        [sys.executable, str(helper), str(driver), mode, reason],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        capture_output=True,
        timeout=12,
    )
    assert result.returncode == 0, result.stderr.decode()[-2000:]
