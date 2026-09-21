"""Check candidate identity and safe invocation around the released YAGA CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import check_repository_policy as policy


@pytest.fixture
def candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=tmp_path, text=True, encoding="utf-8"
        ).strip()

    git("init", "-b", "chore/policies")
    git("config", "user.name", "Policy Test")
    git("config", "user.email", "policy@example.test")
    (tmp_path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-m", "fixture")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setattr(policy, "ROOT", tmp_path)
    for key in ("YAGA_REVISION", "YAGA_BASE", "YAGA_BRANCH", "YAGA_FORMAT"):
        monkeypatch.delenv(key, raising=False)
    calls: list[list[str]] = []
    outcomes = [0, 0, 0]
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[0] != "uvx":
            return original_run(command, **kwargs)
        calls.append(command)
        return subprocess.CompletedProcess(command, outcomes[len(calls) - 1])

    monkeypatch.setattr(policy.subprocess, "run", run)
    return git, calls, outcomes


def test_candidate_uses_exact_git_identity(candidate):
    git, calls, _ = candidate
    assert policy.main() == 0
    head = git("rev-parse", "HEAD")
    assert calls[0][calls[0].index("--revision") + 1] == head
    assert calls[1][calls[1].index("--name") + 1] == "chore/policies"
    assert calls[2][calls[2].index("--range") + 1] == f"{head}...{head}"


@pytest.mark.parametrize("staged", [False, True])
def test_dirty_candidate_stops_before_policy_execution(candidate, tmp_path, staged):
    git, calls, _ = candidate
    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    if staged:
        git("add", "tracked.txt")
    assert policy.main() == 2
    assert calls == []


def test_other_revision_cannot_attest_current_workflow_bytes(candidate, monkeypatch, tmp_path):
    git, calls, _ = candidate
    old = git("rev-parse", "HEAD")
    (tmp_path / "tracked.txt").write_text("new commit\n", encoding="utf-8")
    git("commit", "-am", "second fixture")
    monkeypatch.setenv("YAGA_REVISION", old)
    assert policy.main() == 2
    assert calls == []


def test_detached_checkout_requires_explicit_branch(candidate, monkeypatch):
    git, calls, _ = candidate
    git("checkout", "--detach")
    assert policy.main() == 2
    assert calls == []
    monkeypatch.setenv("YAGA_BRANCH", "chore/policies")
    assert policy.main() == 0


def test_untrusted_branch_is_one_literal_argument(candidate, monkeypatch):
    _, calls, _ = candidate
    branch = "feat/$(echo injected)"
    monkeypatch.setenv("YAGA_BRANCH", branch)
    assert policy.main() == 0
    assert calls[1][calls[1].index("--name") + 1] == branch


@pytest.mark.parametrize("results,expected", [([1, 0, 0], 1), ([1, 2, 0], 2), ([0, -9, 1], 2)])
def test_all_checks_run_and_preserve_failure_severity(candidate, results, expected):
    _, calls, outcomes = candidate
    outcomes[:] = results
    assert policy.main() == expected
    assert len(calls) == 3


def test_missing_base_is_an_input_error(candidate, monkeypatch):
    _, calls, _ = candidate
    monkeypatch.setenv("YAGA_BASE", "missing-base")
    assert policy.main() == 2
    assert calls == []
