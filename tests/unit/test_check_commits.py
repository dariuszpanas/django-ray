"""Consumer entry-point errors and literal argument handling."""

from __future__ import annotations

import subprocess

import pytest

from scripts import check_commits


def test_missing_title_does_not_invoke_a_tool(monkeypatch) -> None:
    monkeypatch.delenv("PR_TITLE", raising=False)
    with pytest.raises(SystemExit) as error:
        check_commits.main(["title"])
    assert error.value.code == 2


def test_title_is_one_literal_argument_and_failure_propagates(monkeypatch) -> None:
    title = 'fix: preserve literal $(input) and "quoted" text'
    monkeypatch.setenv("PR_TITLE", title)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 2)

    monkeypatch.setattr(check_commits.subprocess, "run", run)
    assert check_commits.main(["title"]) == 2
    assert calls[0][0][-2:] == ["--title", title]
    assert not calls[0][1].get("shell", False)


def test_empty_range_is_success_only_after_valid_git_resolution(monkeypatch) -> None:
    responses = iter(["a" * 40, "a" * 40, ""])
    monkeypatch.setattr(check_commits, "git_output", lambda *args: next(responses))
    assert check_commits.main(["range"]) == 0

    def invalid(*args):
        raise subprocess.CalledProcessError(128, ["git", *args])

    monkeypatch.setattr(check_commits, "git_output", invalid)
    with pytest.raises(subprocess.CalledProcessError):
        check_commits.main(["range", "--base", "missing"])


@pytest.mark.parametrize("mode", ["file", "edit"])
def test_file_and_editor_inputs_keep_distinct_semantics(tmp_path, monkeypatch, mode) -> None:
    message = tmp_path / "message with spaces.txt"
    message.write_text("; template guidance\n", encoding="utf-8")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(check_commits.subprocess, "run", run)
    assert check_commits.main([mode, str(message)]) == 1
    assert calls[0][-2:] == [f"--{mode}", str(message.resolve())]
    assert message.read_text(encoding="utf-8") == "; template guidance\n"
