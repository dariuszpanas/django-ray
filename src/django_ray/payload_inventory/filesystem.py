"""Descriptor-relative metadata traversal; never open a payload for reading."""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from django_ray.doctor.probe import ProbeUnavailableError
from django_ray.payload_inventory.contracts import OUTCOMES, Limits

_HEX_PAIR = re.compile(r"[0-9a-f]{2}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\.json\Z")


@contextmanager
def open_directory(path: str):
    """Refuse symlinks in every root component, not just the final component."""
    if ".." in path.split("/"):
        # Lexically collapsing link/.. can select a different namespace from
        # runtime Path.resolve(). Refuse ambiguity before opening any directory.
        raise OSError("Ambiguous inventory filesystem root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open("/", flags)
    try:
        # abspath freezes relative configuration without resolving symlink aliases.
        for component in Path(os.path.abspath(path)).parts[1:]:
            child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
        yield current
    finally:
        os.close(current)


class Scan:
    def __init__(self, *, limits: Limits, deadline: float, emit: Callable[[dict], None]):
        self.limits = limits
        self.deadline = deadline
        self.emit = emit
        self.entries = 0
        self.objects = 0
        self.details = 0
        self.counts = dict.fromkeys(OUTCOMES, 0)
        self.cutoff = time.time() - limits.grace_hours * 3600

    def check(self):
        if time.monotonic() >= self.deadline:
            raise ProbeUnavailableError("probe_timeout")

    def record(self, relative: str, outcome: str):
        self.counts[outcome] += 1
        if self.details < self.limits.max_details:
            self.emit(
                {
                    "kind": "detail",
                    "object_sha256": hashlib.sha256(
                        relative.encode("utf-8", "surrogatepass")
                    ).hexdigest(),
                    "outcome": outcome,
                }
            )
            self.details += 1

    def walk(self, directory: int, parts: tuple[str, ...], lookup: Callable):
        self.check()
        with os.scandir(directory) as entries:
            for entry in entries:
                self.check()
                if self.entries >= self.limits.max_entries:
                    raise ProbeUnavailableError("entry_budget_exhausted")
                self.entries += 1
                name = entry.name
                relative = "/".join((*parts, name))
                try:
                    metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if len(parts) < 2:
                        if not _HEX_PAIR.fullmatch(name) or not stat.S_ISDIR(metadata.st_mode):
                            self.record(relative, "malformed")
                            continue
                        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                        child = os.open(name, flags, dir_fd=directory)
                        try:
                            self.walk(child, (*parts, name), lookup)
                        finally:
                            os.close(child)
                        continue
                    if (
                        not _DIGEST.fullmatch(name)
                        or parts != (name[:2], name[2:4])
                        or not stat.S_ISREG(metadata.st_mode)
                    ):
                        self.record(relative, "malformed")
                        continue
                    if self.objects >= self.limits.max_objects:
                        raise ProbeUnavailableError("object_budget_exhausted")
                    self.objects += 1
                    size, modified = metadata.st_size, metadata.st_mtime
                    if (
                        type(size) is not int
                        or not 0 <= size < 2**63
                        or not math.isfinite(modified)
                        or modified < 0
                        or modified > time.time()
                        or metadata.st_nlink != 1
                    ):
                        self.record(relative, "ambiguous")
                        continue
                    digest = name[:-5]
                    reference = f"resultfs://sha256/{digest}?rel={relative}&bytes={size}"
                    outcome = lookup(reference, digest, size)
                    if outcome is None:
                        outcome = (
                            "unregistered_aged"
                            if modified <= self.cutoff
                            else "unregistered_recent"
                        )
                    self.record(relative, outcome)
                except OSError:
                    # A vanished/replaced entry is an observation, never a missing-object proof.
                    self.record(relative, "unavailable")

    def run(self, path: str, lookup: Callable):
        code = "observed"
        try:
            with open_directory(path) as directory:
                if self.limits.shard:
                    self.shard(directory, lookup)
                else:
                    self.walk(directory, (), lookup)
        except ProbeUnavailableError as error:
            code = error.code
        except OSError:
            code = "filesystem_unavailable"
        if code == "observed" and self.counts["unavailable"]:
            code = "filesystem_unavailable"
        self.finish(code)

    def shard(self, directory: int, lookup: Callable):
        assert self.limits.shard is not None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        opened = []
        try:
            for component in (self.limits.shard[:2], self.limits.shard[2:]):
                self.check()
                if self.entries >= self.limits.max_entries:
                    raise ProbeUnavailableError("entry_budget_exhausted")
                self.entries += 1
                try:
                    directory = os.open(component, flags, dir_fd=directory)
                except FileNotFoundError:
                    return
                opened.append(directory)
            self.walk(directory, (self.limits.shard[:2], self.limits.shard[2:]), lookup)
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    def finish(self, code: str):
        self.emit(
            {
                "kind": "summary",
                "code": code,
                "entries": self.entries,
                "objects": self.objects,
                "counts": self.counts,
            }
        )


def registry_lookup(
    reference: str, digest: str, size: int, *, using: str = "default"
) -> str | None:
    """Fetch at most two scalar classifications, including the rq2 alias."""
    from django.db.models import Case, CharField, Q, Value, When

    from django_ray.execution_codec import EXECUTION_REQUEST_SCHEMA_VERSION
    from django_ray.input_storage import INPUT_ENVELOPE_VERSION
    from django_ray.models import TaskInputPayload

    wrapped = "rq2ref:v1:" + reference
    # Registry identity contains metadata, but physical ownership does not: a
    # truncated/replaced object still belongs to its old byte-count reference.
    # Return only bounded scalar observations, never hydrate corrupt URI text.
    prefix = reference.rsplit("&bytes=", 1)[0] + "&bytes="
    shape = Q(
        reference__in=(reference, wrapped), backend="filesystem", digest=digest, size_bytes=size
    ) & (
        Q(reference=reference, payload_kind="task_input", envelope_version=INPUT_ENVELOPE_VERSION)
        | Q(payload_kind="ray_job_request", envelope_version=EXECUTION_REQUEST_SCHEMA_VERSION)
    )
    rows = list(
        TaskInputPayload.objects.using(using)
        .filter(Q(reference__startswith=prefix) | Q(reference__startswith="rq2ref:v1:" + prefix))
        .order_by()
        .annotate(
            observation=Case(
                When(shape & Q(state="ACTIVE"), then=Value("registered_active")),
                When(shape & Q(state="PURGED"), then=Value("registered_tombstone")),
                default=Value("ambiguous"),
                output_field=CharField(),
            )
        )
        .values_list("observation", flat=True)[:2]
    )
    if len(rows) > 1:
        return "ambiguous"
    return rows[0] if rows else None
