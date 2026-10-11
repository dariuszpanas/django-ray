"""The installed-doctor fixture owns and removes all of its resources."""

from __future__ import annotations

import sys

import pytest

from scripts import verify_doctor_wheel, verify_wheel


def test_doctor_verifier_hook_loads_owned_sibling_under_isolated_import(monkeypatch):
    calls = []
    original = verify_wheel.importlib.util.spec_from_file_location

    def spec(name, path):
        calls.append(path.name)
        value = original(name, path)
        execute = value.loader.exec_module

        def load(module):
            execute(module)
            module.verify = lambda: calls.append("verified")

        monkeypatch.setattr(value.loader, "exec_module", load)
        return value

    monkeypatch.setattr(verify_wheel.importlib.util, "spec_from_file_location", spec)
    verify_wheel._verify_doctor()
    assert calls == ["verify_doctor_wheel.py", "verified"]


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Installed doctor execution is Linux only"
)
def test_installed_doctor_fixture_proves_readonly_observations_and_cleanup(capsys):
    verify_doctor_wheel.verify()
    assert "read-only preservation and cleanup passed" in capsys.readouterr().out


def test_installed_doctor_fixture_skips_runtime_on_nonlinux(monkeypatch):
    monkeypatch.setattr(verify_doctor_wheel.sys, "platform", "win32")
    monkeypatch.setattr(
        verify_doctor_wheel,
        "TemporaryDirectory",
        lambda **_kwargs: pytest.fail("No non-Linux execution"),
    )
    verify_doctor_wheel.verify()
