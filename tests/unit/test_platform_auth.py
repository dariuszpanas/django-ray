"""A partial or unbounded platform observation cannot become a passing receipt."""

import json
from unittest.mock import Mock

import pytest

from qualification import platform_auth
from scripts.local_kuberay_auth import RAY_AUTH_RECEIPT_KEYS


def _kube(monkeypatch):
    monkeypatch.setattr(platform_auth.time, "monotonic", lambda: 100)
    receipt = {"schema_version": 1, **dict.fromkeys(RAY_AUTH_RECEIPT_KEYS, True)}
    return Mock(deadline=400, call=Mock(return_value=json.dumps(receipt))), receipt


def test_uses_owned_head_and_independently_bounded_existing_probe(monkeypatch):
    kube, receipt = _kube(monkeypatch)
    assert platform_auth.observe(kube, "ray-head-owned") == receipt
    args, kwargs = kube.call.call_args
    assert args[:7] == ("exec", "ray-head-owned", "-c", "ray-head", "--", "python", "-c")
    assert kwargs == {"timeout": 70}
    assert args[7].startswith("import signal\nsignal.alarm(65)\n")
    assert args[7].endswith(platform_auth.ray_auth_probe_script())
    assert "ray.init(" not in args[7]
    assert "submit_job(" not in args[7]
    assert "RAY_AUTH_TOKEN" in args[7]  # The child reads its existing environment.


@pytest.mark.parametrize("head", ["", "-head", "head/other", "x" * 64, "head\nother"])
def test_invalid_pod_identity_never_executes(monkeypatch, head):
    kube, _ = _kube(monkeypatch)
    with pytest.raises(ValueError, match="Invalid owned"):
        platform_auth.observe(kube, head)
    kube.call.assert_not_called()


def test_reserves_following_phase_before_starting(monkeypatch):
    kube, _ = _kube(monkeypatch)
    kube.deadline = 319
    with pytest.raises(TimeoutError, match="Insufficient"):
        platform_auth.observe(kube, "ray-head-owned")
    kube.call.assert_not_called()


@pytest.mark.parametrize("field", sorted(RAY_AUTH_RECEIPT_KEYS))
@pytest.mark.parametrize("value", [False, 1, None])
def test_every_auth_fact_must_be_literal_true(monkeypatch, field, value):
    kube, receipt = _kube(monkeypatch)
    receipt[field] = value
    kube.call.return_value = json.dumps(receipt)
    with pytest.raises(ValueError, match="^Ray authentication observation failed$"):
        platform_auth.observe(kube, "ray-head-owned")


@pytest.mark.parametrize("output", ["private-token", "x" * 4097, "null", "{}", b"{}"])
def test_malformed_or_oversized_output_is_not_exposed(monkeypatch, output):
    kube, _ = _kube(monkeypatch)
    kube.call.return_value = output
    with pytest.raises(ValueError, match="^Ray authentication observation failed$"):
        platform_auth.observe(kube, "ray-head-owned")


def test_transport_error_does_not_expose_exception_text(monkeypatch):
    kube, _ = _kube(monkeypatch)
    kube.call.side_effect = RuntimeError("private-token")
    with pytest.raises(ValueError, match="^Ray authentication observation failed$") as error:
        platform_auth.observe(kube, "ray-head-owned")
    assert error.value.__suppress_context__ is True


def test_completed_child_cannot_consume_following_budget(monkeypatch):
    kube, _ = _kube(monkeypatch)
    clock = iter((100, 251))
    monkeypatch.setattr(platform_auth.time, "monotonic", lambda: next(clock))
    with pytest.raises(ValueError, match="^Ray authentication observation failed$"):
        platform_auth.observe(kube, "ray-head-owned")
