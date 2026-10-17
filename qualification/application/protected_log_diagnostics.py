"""Finite failure facts; never render private exception or subprocess text."""

from __future__ import annotations

import json
import subprocess
import sys
from contextvars import ContextVar
from functools import wraps

MAX_BYTES = 512
PHASES = {
    "host": frozenset(
        {
            "inventory",
            "core_receipt",
            "status_check",
            "web_log",
            "manager_log",
            "private_compare",
            "final_inventory",
            "unavailable",
        }
    ),
    "child": frozenset(
        {"input", "django_setup", "read_only_db", "status_api", "private_compare", "unavailable"}
    ),
}
TYPES = frozenset({"timeout", "invalid", "unavailable"})
_STATE = ContextVar("protected_log_failure_phase", default=None)
_ATTRIBUTE = "_protected_log_failure"


def failure_type(error: Exception) -> str:
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    http = sys.modules.get("qualification.application.run_api")
    http_error = getattr(http, "ApplicationHttpError", None)
    transports = getattr(http, "HttpTransportFailure", None)
    timeout = getattr(transports, "TIMEOUT", None)
    if (
        isinstance(http_error, type)
        and issubclass(http_error, Exception)
        and isinstance(transports, type)
        and timeout is not None
        and isinstance(error, http_error)
        and getattr(error, "transport_failure", None) is timeout
    ):
        return "timeout"
    return "invalid" if isinstance(error, (ValueError, TypeError, KeyError)) else "unavailable"


def _record(scope: str, phase: str, kind: str, child_phase: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "layer": "application_protected_logs",
        "status": "failed",
        "failure_scope": scope,
        "failure_phase": phase,
        "failure_type": kind,
        "child_phase": child_phase,
    }


def _valid(value: object, scope: str) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(_record(scope, "unavailable", "unavailable"))
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["layer"] == "application_protected_logs"
        and value["status"] == "failed"
        and value["failure_scope"] == scope
        and type(value["failure_phase"]) is str
        and value["failure_phase"] in PHASES[scope]
        and type(value["failure_type"]) is str
        and value["failure_type"] in TYPES
        and (
            value["child_phase"] is None
            or (
                scope == "host"
                and type(value["child_phase"]) is str
                and value["child_phase"] in PHASES["child"]
            )
        )
    )


class ChildFailureError(ValueError):
    """Only an exact bounded child failure may annotate a failed host command."""

    def __init__(self, value: dict):
        if not _valid(value, "child"):
            raise ValueError("Invalid protected log child diagnostic")
        super().__init__("Protected log private comparison did not pass")
        self.phase = value["failure_phase"]
        self.kind = value["failure_type"]


def parse_child(raw: bytes) -> ChildFailureError | None:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        return None

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate diagnostic field")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
        return ChildFailureError(value)
    except (ValueError, TypeError, KeyError):
        return None


def phase(name: str) -> None:
    state = _STATE.get()
    if state is not None:
        if name not in PHASES[state["scope"]]:
            raise ValueError("Invalid protected log diagnostic phase")
        state["phase"] = name


def tracked(scope: str):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            state = {"scope": scope, "phase": "unavailable"}
            token = _STATE.set(state)
            try:
                return function(*args, **kwargs)
            except Exception as error:
                child = error if isinstance(error, ChildFailureError) else None
                error.__dict__[_ATTRIBUTE] = _record(
                    scope,
                    state["phase"],
                    child.kind if child else failure_type(error),
                    child.phase if child else None,
                )
                raise
            finally:
                _STATE.reset(token)

        return run

    return decorate


def diagnostic(error: Exception, scope: str) -> str:
    value = error.__dict__.get(_ATTRIBUTE)
    if not _valid(value, scope):
        value = _record(scope, "unavailable", failure_type(error))
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
