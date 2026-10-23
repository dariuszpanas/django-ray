"""Resource-free platform and process-ownership checks for preparation benchmarks."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import benchmark_workflow_progress_preparation as benchmark


@pytest.mark.parametrize("platform", ["win32", "darwin", "freebsd14"])
@pytest.mark.parametrize("arguments", [[], ["--worker"]])
def test_cli_rejects_nonlinux_before_preparation_or_process_side_effects(
    monkeypatch: pytest.MonkeyPatch, platform: str, arguments: list[str]
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "argv", ["benchmark", *arguments])
    forbidden = Mock(side_effect=AssertionError("platform rejection must precede work"))
    for helper in (
        "_worker",
        "_validated_nodes",
        "_source_snapshot",
        "_forced_termination_probe",
        "_make_scenario_root",
        "_write_report",
    ):
        monkeypatch.setattr(benchmark, helper, forbidden)
    monkeypatch.setattr(benchmark.subprocess, "Popen", forbidden)

    with pytest.raises(SystemExit, match="requires Linux"):
        benchmark.main()

    forbidden.assert_not_called()


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_private_launcher_rejects_nonlinux_before_spawning(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    launch = Mock(side_effect=AssertionError("unsupported host must not spawn"))
    monkeypatch.setattr(benchmark.subprocess, "Popen", launch)

    with pytest.raises(SystemExit, match="requires Linux"):
        benchmark._launch_worker(["unused-worker"])

    launch.assert_not_called()


def test_linux_launcher_owns_a_new_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    launch = Mock()
    monkeypatch.setattr(benchmark.subprocess, "Popen", launch)
    command = ["unused-worker"]

    assert benchmark._launch_worker(command) is launch.return_value
    launch.assert_called_once_with(
        command,
        cwd=benchmark.ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def test_cleanup_kills_owned_descendants_after_launcher_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=123, returncode=0)
    process.poll.return_value = 0
    kill_group = Mock()
    monkeypatch.setattr(benchmark.os, "killpg", kill_group, raising=False)
    monkeypatch.setattr(benchmark.signal, "SIGKILL", 9, raising=False)

    benchmark._terminate_process_tree(process)

    kill_group.assert_called_once_with(123, 9)
    process.communicate.assert_called_once_with(timeout=10.0)
    process.kill.assert_not_called()


def test_cleanup_reaps_launcher_when_owned_group_is_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=123)
    monkeypatch.setattr(benchmark.os, "killpg", Mock(side_effect=ProcessLookupError), raising=False)
    monkeypatch.setattr(benchmark.signal, "SIGKILL", 9, raising=False)

    benchmark._terminate_process_tree(process)

    process.communicate.assert_called_once_with(timeout=10.0)
    process.kill.assert_not_called()


def test_cleanup_reports_group_failure_after_reaping_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=123)
    process.poll.return_value = None
    failure = PermissionError("owned group could not be signalled")
    monkeypatch.setattr(benchmark.os, "killpg", Mock(side_effect=failure), raising=False)
    monkeypatch.setattr(benchmark.signal, "SIGKILL", 9, raising=False)

    with pytest.raises(RuntimeError, match="failed to terminate") as caught:
        benchmark._terminate_process_tree(process)

    assert caught.value.__cause__ is failure
    process.kill.assert_called_once_with()
    process.communicate.assert_called_once_with(timeout=10.0)


def test_cleanup_has_a_bounded_retry_for_an_unresponsive_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=123)
    process.communicate.side_effect = subprocess.TimeoutExpired("worker", 10.0)
    monkeypatch.setattr(benchmark.os, "killpg", Mock(), raising=False)
    monkeypatch.setattr(benchmark.signal, "SIGKILL", 9, raising=False)

    with pytest.raises(RuntimeError, match="owned benchmark launcher did not terminate"):
        benchmark._terminate_process_tree(process)

    assert process.communicate.call_count == 2
    process.kill.assert_called_once_with()


def test_worker_timeout_preserves_diagnostics_after_tree_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = ["unused-worker"]
    process = Mock()
    process.communicate.side_effect = subprocess.TimeoutExpired(
        command, 1.0, output="partial stdout", stderr="partial stderr"
    )
    monkeypatch.setattr(benchmark, "_launch_worker", lambda _command: process)
    cleanup = Mock()
    monkeypatch.setattr(benchmark, "_terminate_process_tree", cleanup)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        benchmark._run_worker_process(command, timeout_seconds=1.0)

    cleanup.assert_called_once_with(process)
    assert caught.value.output == "partial stdout"
    assert caught.value.stderr == "partial stderr"


def test_rss_converts_linux_kib_to_report_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda _who: SimpleNamespace(ru_maxrss=123))
    monkeypatch.setitem(sys.modules, "resource", resource)

    assert benchmark._peak_resource_rss() == 123 * 1024
