"""Admission for the finite primitive snapshot consumed by terminal preparation."""

from __future__ import annotations

import json
import math
from typing import Any

TERMINAL_SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024
TERMINAL_SNAPSHOT_MAX_VALUES = 131_072
TERMINAL_SNAPSHOT_MAX_DEPTH = 16


class TerminalSnapshotInvalidError(ValueError):
    """The snapshot is not an exact, finite JSON primitive tree."""


class TerminalSnapshotLimitError(ValueError):
    """The snapshot exceeds the terminal preparation admission budget."""


def admit_terminal_snapshot(value: Any) -> int:
    """Count canonical UTF-8 bytes without copying or serializing the tree.

    Only bounded scalar strings are encoded. Container traversal has independent
    depth and value limits, including keys, so cycles and wide empty structures
    cannot exhaust the stack or bypass admission with a small byte count.
    This bounds preparation work; it does not bound Ray's prior deserialization.
    """
    remaining_bytes = TERMINAL_SNAPSHOT_MAX_BYTES
    remaining_values = TERMINAL_SNAPSHOT_MAX_VALUES

    def consume(amount: int) -> None:
        nonlocal remaining_bytes
        if amount > remaining_bytes:
            raise TerminalSnapshotLimitError
        remaining_bytes -= amount

    def visit(item: Any, depth: int) -> None:
        nonlocal remaining_values
        if depth > TERMINAL_SNAPSHOT_MAX_DEPTH or remaining_values == 0:
            raise TerminalSnapshotLimitError
        remaining_values -= 1
        kind = type(item)
        if item is None:
            consume(4)
        elif kind is bool:
            consume(4 if item else 5)
        elif kind is int:
            if not -(1 << 63) <= item < (1 << 63):
                raise TerminalSnapshotInvalidError
            consume(len(str(item)))
        elif kind is float:
            if not math.isfinite(item):
                raise TerminalSnapshotInvalidError
            consume(len(json.dumps(item, allow_nan=False)))
        elif kind is str:
            # UTF-8 JSON needs at least one byte per character plus two quotes.
            # Refuse before allocating an encoding for an oversized scalar.
            if len(item) + 2 > remaining_bytes:
                raise TerminalSnapshotLimitError
            try:
                consume(len(json.dumps(item, ensure_ascii=False).encode("utf-8")))
            except UnicodeError as error:
                raise TerminalSnapshotInvalidError from error
        elif kind is list or kind is dict:
            count = len(item)
            if count * (2 if kind is dict else 1) > remaining_values:
                raise TerminalSnapshotLimitError
            consume(2 + max(0, count - 1) + (count if kind is dict else 0))
            if kind is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise TerminalSnapshotInvalidError
                    visit(key, depth + 1)
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
        else:
            raise TerminalSnapshotInvalidError

    visit(value, 0)
    return TERMINAL_SNAPSHOT_MAX_BYTES - remaining_bytes
