"""Spelling gate input and failure propagation boundaries."""

from __future__ import annotations

import subprocess

import pytest

from scripts import check_spelling


def test_spelling_checks_new_files_and_propagates_findings(tmp_path, monkeypatch) -> None:
    (tmp_path / "new file.md").write_text("content", encoding="utf-8")
    (tmp_path / "tracked.md").write_text("content", encoding="utf-8")
    monkeypatch.setattr(check_spelling, "ROOT", tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "git":
            return subprocess.CompletedProcess(
                args, 0, b"tracked.md\0deleted.md\0new file.md\0tracked.md\0"
            )
        return subprocess.CompletedProcess(args, 2)

    monkeypatch.setattr(check_spelling.subprocess, "run", run)
    assert check_spelling.main() == 2
    assert calls[1][1]["input"] == b"new file.md\ntracked.md\n"
    assert "--isolated" in calls[1][0]
    assert "--others" in calls[0][0]


def test_spelling_does_not_hide_git_errors(monkeypatch) -> None:
    def run(args, **kwargs):
        raise subprocess.CalledProcessError(128, args)

    monkeypatch.setattr(check_spelling.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        check_spelling.main()
