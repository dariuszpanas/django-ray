"""Small resource-free checks for the sender-only measurement contract."""

from __future__ import annotations

import sys

import pytest

from scripts import benchmark_distributed_preparation as benchmark


@pytest.mark.parametrize("helper", ["map", "starmap", "scatter"])
def test_measurement_uses_a_nonexecuting_sink_and_observes_preparation(helper: str) -> None:
    result = benchmark._measure(helper, 5, 2)
    assert result["requests"] == 5
    assert result["full_encodings"] == 1
    assert result["callable_hashes"] == (5 if helper == "scatter" else 1)
    assert result["max_prepared_ahead"] == 2
    assert result["effective_window"] == 2
    assert len(result["wire_sha256"]) == 64
    assert result["python_peak_bytes"] > 0
    assert result["wall_seconds"] >= result["first_submit_seconds"] >= 0


@pytest.mark.parametrize(
    "arguments",
    [
        ["--items", "0"],
        ["--items", "5001"],
        ["--items", "1", "1"],
        ["--window", "257"],
        ["--repetitions", "6"],
        ["--case-timeout", "181"],
    ],
)
def test_cli_refuses_unbounded_profiles(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["benchmark", *arguments])
    with pytest.raises(SystemExit) as caught:
        benchmark.main()
    assert caught.value.code == 2


def test_cli_refuses_nonlinux_benchmark_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["benchmark"])
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SystemExit) as caught:
        benchmark.main()
    assert caught.value.code == 2
