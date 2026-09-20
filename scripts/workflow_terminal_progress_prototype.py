"""Non-production experiment for a bounded secondary Ray task result.

This does not implement workflow-wide admission. A future coordinator must own
the reservation before dispatch and retain it through consumption/acknowledgement.
The callback injection here is fixture-only, not a proposed user task signature.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

MAX_METADATA_BYTES = 1024


def identity_encoder(value: bytes) -> bytes:
    return value


def execute_with_metadata(
    callback: Callable[..., Any],
    *args: Any,
    admitted: bool,
    encoder: Callable[[bytes], bytes] = identity_encoder,
) -> Any:
    """Preserve callback exceptions; make metadata failure observational only."""
    latest: bytes | None = None

    def report(wire: bytes) -> bool:
        nonlocal latest
        if not admitted or type(wire) is not bytes or len(wire) > MAX_METADATA_BYTES:
            return False
        latest = wire
        return True

    # Do not catch the callable's exception: Ray's configured retry policy owns
    # it, exactly as for an ordinary one-result remote function.
    result = callback(*args, report=report)
    if not admitted:
        return result
    metadata = None
    if latest is not None:
        try:
            candidate = encoder(latest)
            if type(candidate) is bytes and len(candidate) <= MAX_METADATA_BYTES:
                metadata = candidate
        except Exception:
            # An observational encoder failure must not turn success into a
            # failed/retried application invocation.
            pass
    return result, metadata
