"""One harmless invocation sentinel for the owned unsupported-protocol probe."""

from pathlib import Path
from uuid import UUID


def poison(generation: str, task_id: str):
    """Any invocation violates the strict refusal boundary and leaves a marker."""
    if generation not in {"before", "after"} or str(UUID(task_id)) != task_id:
        raise ValueError("Invalid protocol fixture identity")
    root = Path("/rq2") / f"protocol-{generation}-{task_id}"
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ValueError("Protocol fixture mount is unavailable")
    with (root / "invoked").open("xb") as stream:
        stream.write(b"invoked")
    return "protocol-qualification-poison-invoked"
