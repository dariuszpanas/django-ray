"""Bound the existing read-only Ray authentication probe in an owned pod."""

from __future__ import annotations

import json
import re
import time
from typing import Protocol

from scripts.local_kuberay_auth import ray_auth_probe_script, validate_auth_receipt

CHILD_SECONDS = 65
COMMAND_SECONDS = 70
FOLLOWING_RESERVE_SECONDS = 150
MAX_RECEIPT_BYTES = 4096


class OwnedKubernetes(Protocol):
    deadline: float

    def call(self, *args: str, timeout: float = 30) -> str: ...


def observe(kube: OwnedKubernetes, head: str) -> dict:
    """Require every fixed fact without exposing credentials or response bodies.

    The Linux child has its own alarm: timing out kubectl alone would leave
    remote execution unbounded. No task, actor or child process is created by
    the probe. Preserve the existing browser/cleanup observation reserve.
    """
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", head) is None:
        raise ValueError("Invalid owned Ray head name")
    if kube.deadline - time.monotonic() < COMMAND_SECONDS + FOLLOWING_RESERVE_SECONDS:
        raise TimeoutError("Insufficient Ray authentication observation budget")
    script = f"import signal\nsignal.alarm({CHILD_SECONDS})\n" + ray_auth_probe_script()
    try:
        output = kube.call(
            "exec",
            head,
            "-c",
            "ray-head",
            "--",
            "python",
            "-c",
            script,
            timeout=COMMAND_SECONDS,
        )
        if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_RECEIPT_BYTES:
            raise ValueError("Invalid authentication receipt size")
        receipt = json.loads(output)
        validate_auth_receipt(receipt, surface="ray")
        if kube.deadline - time.monotonic() < FOLLOWING_RESERVE_SECONDS:
            raise TimeoutError("Authentication consumed the following observation reserve")
    except Exception:
        raise ValueError("Ray authentication observation failed") from None
    return receipt
