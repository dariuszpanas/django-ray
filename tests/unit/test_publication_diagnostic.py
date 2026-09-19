"""Temporary qualification instrumentation must not print exception contents."""

import json

import pytest

from django_ray.workflow.progress.publication import _diagnose_qualification_publication


@pytest.mark.parametrize("qualified", [False, True])
def test_diagnostic_omits_exception_contents(monkeypatch, capsys, qualified):
    monkeypatch.setenv(
        "DJANGO_SETTINGS_MODULE",
        "testproject.settings_qualification" if qualified else "testproject.settings",
    )
    # Windows is not the native execution target; emulate only the uid read.
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    try:
        raise ValueError("private-exception-content-canary")
    except ValueError as error:
        _diagnose_qualification_publication(error)
    output = capsys.readouterr().out
    assert "private-exception-content-canary" not in output
    if qualified:
        payload = json.loads(output)
        assert payload["type"] == "ValueError"
        assert set(payload) == {"layer", "type", "temporary_parent"}
    else:
        assert output == ""
