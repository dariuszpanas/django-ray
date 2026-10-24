"""Complete-trace checks, separate from retaining finite failed observations."""

from __future__ import annotations

import re
from typing import Any

from scripts.runtime_env_growth.evidence import (
    DEFAULT_EXCLUDES,
    EXCLUDES,
    MAX_BYTES,
    MAX_ENTRIES,
    PHASES,
    GrowthError,
    digest,
    encode,
)


def complete_trace(records: list[dict[str, Any]], arm: str) -> None:
    """A producer's passed flag cannot replace the fixed six-session evidence."""
    cursor = 0

    def take() -> dict[str, Any]:
        nonlocal cursor
        if cursor >= len(records):
            raise GrowthError("report_invalid")
        result = records[cursor]
        cursor += 1
        if not isinstance(result, dict):
            raise GrowthError("report_invalid")
        if any(key in result and type(result[key]) is not int for key in ("session", "index")):
            raise GrowthError("report_invalid")
        return result

    def data(session: int) -> dict[str, Any]:
        event = take()
        if set(event) != {"kind", "session", "value"} or (
            event["kind"] != "data"
            or event["session"] != session
            or not isinstance(event["value"], dict)
        ):
            raise GrowthError("report_invalid")
        return event["value"]

    def sha(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None

    def scan(value: object) -> None:
        if (
            not isinstance(value, dict)
            or set(value) != {"status", "entries", "bytes", "roles"}
            or value["status"] != "complete"
            or type(value["entries"]) is not int
            or not 0 <= value["entries"] <= MAX_ENTRIES
            or type(value["bytes"]) is not int
            or not 0 <= value["bytes"] <= MAX_BYTES
        ):
            raise GrowthError("report_invalid")
        roles = value["roles"]
        if not isinstance(roles, dict) or set(roles) != {
            "source",
            "snapshot",
            "upload_scratch",
            "ray_extracted",
            "other",
        }:
            raise GrowthError("report_invalid")
        for counts in roles.values():
            if (
                not isinstance(counts, dict)
                or set(counts)
                != {"bytes", "allocated_bytes", "files", "directories", "symlinks", "special"}
                or any(type(v) is not int or v < 0 for v in counts.values())
            ):
                raise GrowthError("report_invalid")
        if (
            sum(row["bytes"] for row in roles.values()) != value["bytes"]
            or sum(
                sum(row[k] for k in ("files", "directories", "symlinks", "special"))
                for row in roles.values()
            )
            != value["entries"]
        ):
            raise GrowthError("report_invalid")

    metadata = take()
    if set(metadata) != {"kind", "value"} or metadata["kind"] != "data":
        raise GrowthError("report_invalid")
    value = metadata["value"]
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "ray_version",
            "django_ray_version",
            "python_version",
            "settings_sha256",
            "default_excludes",
            "excludes",
            "gitignore_sha256",
            "rayignore_sha256",
        }
        or value["ray_version"] != "2.58.0"
        or value["default_excludes"] != DEFAULT_EXCLUDES
        or value["excludes"] != EXCLUDES
    ):
        raise GrowthError("report_invalid")
    if (
        value["settings_sha256"]
        != digest(
            encode(
                {
                    "defaults": DEFAULT_EXCLUDES,
                    "excludes": EXCLUDES,
                    "gitignore": "build/\n",
                    "rayignore": "dist/\n",
                }
            )
        )
        or value["gitignore_sha256"] != digest(b"build/\n")
        or value["rayignore_sha256"] != digest(b"dist/\n")
        or any(
            not isinstance(value[key], str)
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value[key])
            for key in ("python_version", "django_ray_version")
        )
    ):
        raise GrowthError("report_invalid")
    first_uri = None
    for session in range(1, 4):
        current = None
        control = 0
        for index, phase in enumerate(PHASES):
            if take() != {"kind": "phase", "session": session, "index": index, "phase": phase}:
                raise GrowthError("report_invalid")
            if phase == "prepare":
                if arm == "django" and control not in (1, 4):
                    snapshot = data(session)
                    if set(snapshot) != {"scope", "value"} or snapshot["scope"] != "snapshot":
                        raise GrowthError("report_invalid")
                    scan(snapshot["value"])
                    identity = data(session)
                    if (
                        set(identity) != {"snapshot_removed", "identity_sha256"}
                        or identity["snapshot_removed"] is not True
                        or not sha(identity["identity_sha256"])
                    ):
                        raise GrowthError("report_invalid")
                prepared = data(session)
                operation = (
                    "uri_reuse"
                    if control in (1, 4)
                    else "cache_hit"
                    if control == 2
                    else "uploaded"
                )
                if set(prepared) != {"operation", "archives", "uri_sha256"} or (
                    prepared["operation"] != operation or not sha(prepared["uri_sha256"])
                ):
                    raise GrowthError("report_invalid")
                archives = prepared["archives"]
                scopes = (
                    []
                    if operation == "uri_reuse"
                    else (["snapshot"] if arm == "django" else [])
                    + (["upload_scratch"] if operation == "uploaded" else [])
                )
                if (
                    not isinstance(archives, list)
                    or any(not isinstance(a, dict) for a in archives)
                    or [a.get("scope") for a in archives] != scopes
                ):
                    raise GrowthError("report_invalid")
                for archive in archives:
                    if set(archive) != {
                        "scope",
                        "sha256",
                        "compressed_bytes",
                        "uncompressed_bytes",
                        "entries",
                    } or (
                        not sha(archive["sha256"])
                        or any(
                            type(archive[key]) is not int or archive[key] <= 0
                            for key in ("compressed_bytes", "uncompressed_bytes", "entries")
                        )
                        or archive["entries"] > 1024
                        or max(archive["compressed_bytes"], archive["uncompressed_bytes"])
                        > 16 * 1024**2
                    ):
                        raise GrowthError("report_invalid")
                if control == 0:
                    current = prepared["uri_sha256"]
                    if first_uri is not None and current != first_uri:
                        raise GrowthError("report_invalid")
                    first_uri = current
                if (prepared["uri_sha256"] == current) != (control != 3):
                    raise GrowthError("report_invalid")
            elif phase == "task":
                task = data(session)
                if set(task) != {"variant", "elapsed_seconds"} or task["variant"] != (
                    "B" if control == 3 else "A"
                ):
                    raise GrowthError("report_invalid")
                if (
                    type(task["elapsed_seconds"]) not in (int, float)
                    or not 0 <= task["elapsed_seconds"] <= 35
                ):
                    raise GrowthError("report_invalid")
                control += 1
            elif phase == "scan":
                measured = data(session)
                if index == 1:
                    if (
                        set(measured) != {"scope", "sha256", "value"}
                        or measured["scope"] != "source"
                        or not sha(measured["sha256"])
                    ):
                        raise GrowthError("report_invalid")
                elif index == len(PHASES) - 1:
                    if set(measured) != {"value"}:
                        raise GrowthError("report_invalid")
                else:
                    if set(measured) != {"value", "extracted"}:
                        raise GrowthError("report_invalid")
                    scan(measured["extracted"])
                scan(measured["value"])
        if take() != {"kind": "done", "session": session}:
            raise GrowthError("report_invalid")
    if cursor != len(records):
        raise GrowthError("report_invalid")
