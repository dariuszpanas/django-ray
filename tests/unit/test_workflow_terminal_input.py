from __future__ import annotations

import json

import pytest

from django_ray.workflow.progress import terminal_input


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        0,
        -(1 << 63),
        (1 << 63) - 1,
        1.5,
        'é\n"',
        [],
        {},
        {"items": [None, {"value": "text"}], "count": 2},
    ],
)
def test_admission_counts_canonical_bytes(value: object) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert terminal_input.admit_terminal_snapshot(value) == len(encoded)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), 1 << 63, object(), {1: "value"}, "\ud800"]
)
def test_admission_rejects_nonprimitive_or_invalid_scalars(value: object) -> None:
    with pytest.raises(terminal_input.TerminalSnapshotInvalidError):
        terminal_input.admit_terminal_snapshot(value)


def test_admission_rejects_custom_containers_without_invoking_them() -> None:
    class Hostile(dict):
        def items(self):
            raise AssertionError("custom iteration must not execute")

    with pytest.raises(terminal_input.TerminalSnapshotInvalidError):
        terminal_input.admit_terminal_snapshot(Hostile())


def test_admission_enforces_exact_byte_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_input, "TERMINAL_SNAPSHOT_MAX_BYTES", 8)
    assert terminal_input.admit_terminal_snapshot("ééé") == 8
    with pytest.raises(terminal_input.TerminalSnapshotLimitError):
        terminal_input.admit_terminal_snapshot("éééé")


def test_admission_refuses_huge_string_before_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_input, "TERMINAL_SNAPSHOT_MAX_BYTES", 8)

    def forbidden(*args, **kwargs):
        raise AssertionError("oversized string must not be encoded")

    monkeypatch.setattr(terminal_input.json, "dumps", forbidden)
    with pytest.raises(terminal_input.TerminalSnapshotLimitError):
        terminal_input.admit_terminal_snapshot("x" * 9)


def test_admission_bounds_depth_and_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_input, "TERMINAL_SNAPSHOT_MAX_DEPTH", 2)
    assert terminal_input.admit_terminal_snapshot([[None]]) == 8
    with pytest.raises(terminal_input.TerminalSnapshotLimitError):
        terminal_input.admit_terminal_snapshot([[[None]]])
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(terminal_input.TerminalSnapshotLimitError):
        terminal_input.admit_terminal_snapshot(cycle)


def test_admission_bounds_values_including_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_input, "TERMINAL_SNAPSHOT_MAX_VALUES", 3)
    assert terminal_input.admit_terminal_snapshot({"key": None}) == 12
    with pytest.raises(terminal_input.TerminalSnapshotLimitError):
        terminal_input.admit_terminal_snapshot({"key": [None]})
    with pytest.raises(terminal_input.TerminalSnapshotLimitError):
        terminal_input.admit_terminal_snapshot([None] * 3)
