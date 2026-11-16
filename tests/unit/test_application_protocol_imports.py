"""The optional protocol observer can be imported without loading Ray."""

from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path


def test_protocol_remote_module_import_does_not_import_ray(monkeypatch):
    original = builtins.__import__

    def reject_ray(name, *args, **kwargs):
        if name == "ray" or name.startswith("ray."):
            raise AssertionError("module import loaded optional Ray")
        return original(name, *args, **kwargs)

    path = Path(__file__).resolve().parents[2] / "qualification/application/protocol_remote.py"
    spec = importlib.util.spec_from_file_location("_protocol_remote_import_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(builtins, "__import__", reject_ray)
    spec.loader.exec_module(module)
    assert module.Remote().ray is None
