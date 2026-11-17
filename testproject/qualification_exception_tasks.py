"""Fixed exception-message task, admitted only by the disposable App fixture."""

import os

from django.conf import settings
from django.tasks import task

from django_ray.runtime.context import require_strict_task_execution_context

SETTINGS = frozenset(
    {"testproject.settings_qualification", "testproject.settings_qualification_jobs"}
)
RENDERING_SENTINEL = "qualification exception rendering sentinel"


class BrokenMessageFixtureError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError(RENDERING_SENTINEL)


@task
def broken_message_then_recover() -> dict[str, bool | int]:
    """Fail once by strict durable attempt identity, without a process counter."""
    module = os.environ.get("DJANGO_SETTINGS_MODULE")
    if module not in SETTINGS or settings.SETTINGS_MODULE != module:
        raise ValueError("Exception fixture requires disposable qualification settings")
    current = require_strict_task_execution_context()
    if (
        current.execution_protocol_version != 1
        or current.attempt_number not in {1, 2}
        or current.execution_generation != current.attempt_number
    ):
        raise ValueError("Exception fixture identity is outside its fixed two attempts")
    if current.attempt_number == 1:
        raise BrokenMessageFixtureError()
    return {"recovered": True, "attempt": 2}
