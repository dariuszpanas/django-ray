"""Fixed private exec helper for one bounded Jobs control operation.

The creating manager owns the private IPC directory and external deadline. Only
this fixed dispatcher runs here; request data never selects a Python module or
callable. The manager's consumption nonce remains in its parent process. A local
successful response is an operation result, not a database publication or proof
that the cluster is drained. No command activates production protocol 3.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from django_ray.runner.cohort_process import (
    read_cohort_process_request,
    write_cohort_process_response,
)


def _execute(payload: object) -> dict:
    if (
        type(payload) is not dict
        or payload.keys() != {"command", "arguments"}
        or type(payload["command"]) is not str
        or payload["command"] not in {"prepare", "submit", "inspect", "stop"}
        or type(payload["arguments"]) is not dict
    ):
        raise ValueError("Invalid private Jobs operation")
    from django_ray.runner.cohort_job_control import execute_cohort_job_control

    return execute_cohort_job_control(payload["command"], payload["arguments"])


def main(argv: Sequence[str] | None = None) -> int:
    """Read one private request, execute once and write one bounded response.

    Invalid input never reaches the dispatcher. Exceptions and rejected values
    are deliberately not printed; the owner receives only fixed failure data.
    Signals/interruption remain process failures for the parent's cleanup path.
    """
    arguments = sys.argv[1:] if argv is None else argv
    if (
        type(arguments) not in {list, tuple}
        or len(arguments) != 1
        or type(arguments[0]) is not str
        or not os.path.isabs(arguments[0])
    ):
        return 2
    directory = arguments[0]
    try:
        request = read_cohort_process_request(directory)
    except Exception:
        return 2
    outcome = "ok"
    try:
        payload = _execute(request.payload)
    except Exception:
        outcome = "failed"
        payload = {"reason": "operation_refused"}
    try:
        write_cohort_process_response(
            directory, operation_id=request.operation_id, outcome=outcome, payload=payload
        )
    except Exception:
        return 2
    return 0 if outcome == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
