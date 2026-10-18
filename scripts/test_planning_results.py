"""Offline receipt normalization after a trusted caller authenticates acquisition.

This module does not authenticate GitHub, acquire artifacts, execute tests, or
read metadata from an artifact to establish its own trust. The caller supplies
independently verified job and extracted-file identities. See the shadow guide.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Identifies the reviewed adapter/assertion contract, never the receipt's source.
CONTRACT_PROFILE = "django-ray-45180ea-receipts-v1"
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_FILES = 512
SHA256 = re.compile(r"[0-9a-f]{64}")
GIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
BINDINGS = {
    "application": "application-qualification",
    "native": "upgrade-native-qualification",
    "latency": "latency-qualification",
    "cancellation": "cancellation-qualification",
    "monitoring": "monitoring-qualification",
}
REQUIRED_STEPS = {
    "application": (
        "Require the current source Linux CI checkpoint",
        "Build the committed candidate with a disposable local registry",
        "Run the native application core test",
        "Remove only this job's disposable infrastructure",
    ),
    "native": (
        "Require the current source Linux CI checkpoint",
        "Export exact released and candidate source archives",
        "Rehearse stopped writers, restoration and native execution",
        "Remove owned test resources",
        "Match the runtime archive to the caller's exact Git export",
    ),
    "latency": (
        "Require exact-source Linux CI",
        "Admit disposable runner and export exact source",
        "Measure matched real Job completion windows",
        "Remove owned resources",
    ),
    "cancellation": (
        "Require the current source Linux CI checkpoint",
        "Admit resources and export the exact clean source",
        "Observe no-cancel completion and actual cancelled driver exit",
        "Remove owned resources",
    ),
    "monitoring": (
        "Require the exact-source Linux CI checkpoint",
        "Build the committed candidate before allocating Kind",
        "Observe the shipped monitoring contracts within ten minutes",
        "Remove only this job's disposable infrastructure",
    ),
}


class NormalizationError(ValueError):
    """Incomplete, unsupported or invalid evidence; never a test failure receipt."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise NormalizationError(code)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def sha(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def equal(actual: object, expected: object, code: str) -> None:
    """JSON equality must distinguish booleans from integer acceptance counts."""
    require(canonical(actual) == canonical(expected), code)


@dataclass(frozen=True)
class Cell:
    suite: str
    variant: str
    binding: str

    @property
    def category(self) -> str:
        return self.suite.removeprefix("qualification.")

    def validate(self) -> None:
        require(isinstance(self.suite, str), "invalid-cell")
        category = self.category
        require(self.suite == "qualification." + category and category in BINDINGS, "unknown-cell")
        variants = (
            ("core-loss", "jobs", "jobs-crash", "native") if category == "native" else ("default",)
        )
        require(self.variant in variants and self.binding == BINDINGS[category], "unknown-cell")


@dataclass(frozen=True)
class ExpectedRun:
    plan_digest: str
    policy_sha256: str
    bindings_sha256: str
    execution_commit: str
    execution_tree: str
    run_id: str
    attempt: int
    selected: tuple[Cell, ...]


@dataclass(frozen=True)
class VerifiedJob:
    """One actual execution authenticated independently by the trusted caller.

    ``attempt`` is the attempt that ran this original job ID. A later GitHub
    attempt can contain copied success rows; its enclosing attempt is not proof
    that those jobs executed again. Such rows cannot authorize normalization.
    """

    cell: Cell
    job_id: int
    attempt: int
    outcome: str
    successful_steps: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedFile:
    path: str
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class VerifiedRun:
    """Assertions of an authenticated caller, not a JSON authentication format.

    The caller must verify the exact workflow definitions implement CONTRACT_PROFILE
    for the receipt source, all actual job/source/attempt identities and ZIP API digests, then derive
    the extracted-file inventory from those original ZIP bytes. Names alone do not
    authenticate a job. Do not construct this from PR-controlled receipt claims.
    CONTRACT_PROFILE names reviewed validator code, not the source that executed.
    Every selected job must actually have executed in this exact attempt; there is
    no carry-forward or cross-attempt assembly, even within the same source tree.
    """

    execution_commit: str
    execution_tree: str
    run_id: str
    attempt: int
    contract_profile: str
    jobs: tuple[VerifiedJob, ...]
    files: tuple[VerifiedFile, ...]


def _safe_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 1024
        and re.fullmatch(r"[A-Za-z0-9_./-]+", value) is not None
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _json(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate-json-key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise NormalizationError("nonfinite-json")

    def integer(value):
        require(len(value) <= 64, "oversized-json-number")
        return int(value)

    def floating(value):
        require(len(value) <= 128, "oversized-json-number")
        result = float(value)
        require(math.isfinite(result), "nonfinite-json")
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=nonfinite,
            parse_int=integer,
            parse_float=floating,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise NormalizationError("invalid-json") from None
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        require(depth <= 32 and count <= 50000, "json-budget")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
            pending.extend((key, depth + 1) for key in item)
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError:
                raise NormalizationError("invalid-json-unicode") from None
    return value


class Evidence:
    """Read only bounded regular files whose hashes were authenticated separately."""

    def __init__(self, root: Path, files: tuple[VerifiedFile, ...]):
        require(isinstance(root, Path), "invalid-evidence-root")
        require(isinstance(files, tuple) and len(files) <= MAX_FILES, "file-inventory-budget")
        self.root = root.absolute()
        self.files = {}
        self.used: dict[str, VerifiedFile] = {}
        self.accessed: set[str] = set()
        self.total = 0
        for item in files:
            require(
                isinstance(item, VerifiedFile) and _safe_path(item.path), "invalid-file-inventory"
            )
            require(
                type(item.byte_count) is int
                and 0 <= item.byte_count <= MAX_FILE_BYTES
                and sha(item.sha256)
                and item.path not in self.files,
                "invalid-file-inventory",
            )
            self.files[item.path] = item

    def read(self, path: str) -> bytes:
        require(path in self.files, "missing-authenticated-file")
        item = self.files[path]
        target = self.root
        try:
            # Reject symlinks/junctions in every component, including the root's
            # ancestors. Caller-owned extraction directories must remain private.
            for parent in (*reversed(self.root.parents), self.root):
                require(
                    not parent.is_symlink() and not parent.is_junction(), "linked-evidence-path"
                )
            for part in path.split("/"):
                target /= part
                require(
                    not target.is_symlink() and not target.is_junction(), "linked-evidence-path"
                )
            before = target.lstat()
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            require(
                stat.S_ISREG(before.st_mode)
                and not getattr(before, "st_file_attributes", 0) & reparse,
                "nonregular-evidence",
            )
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(target, flags)
            try:
                opened = os.fstat(descriptor)
                require(
                    stat.S_ISREG(opened.st_mode)
                    and (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino),
                    "changed-evidence-path",
                )
                after = target.lstat()
                require(
                    (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
                    and stat.S_ISREG(after.st_mode)
                    and not getattr(after, "st_file_attributes", 0) & reparse,
                    "changed-evidence-path",
                )
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
            finally:
                os.close(descriptor)
        except OSError:
            raise NormalizationError("unavailable-evidence") from None
        require(
            len(raw) == item.byte_count and hashlib.sha256(raw).hexdigest() == item.sha256,
            "changed-evidence",
        )
        if path not in self.used:
            self.total += len(raw)
            require(self.total <= MAX_TOTAL_BYTES, "evidence-byte-budget")
            self.used[path] = item
        self.accessed.add(path)
        return raw

    def json(self, path: str) -> Any:
        return _json(self.read(path))

    def text(self, path: str) -> str:
        try:
            return self.read(path).decode("utf-8").strip()
        except UnicodeError:
            raise NormalizationError("invalid-text-evidence") from None


def normalize_results(
    artifact_root: Path, *, expected: ExpectedRun, provenance: VerifiedRun
) -> dict:
    """Normalize selected obligations only; invalid evidence is operational error.

    Returned dictionaries follow YAGA test-results v1 and must still be parsed
    and evaluated by YAGA with independently recomputed expected identities.
    """
    require(
        isinstance(expected, ExpectedRun) and isinstance(provenance, VerifiedRun), "invalid-context"
    )
    require(
        all(
            sha(getattr(expected, field))
            for field in ("plan_digest", "policy_sha256", "bindings_sha256")
        ),
        "invalid-context-digest",
    )
    require(
        all(
            isinstance(getattr(expected, field), str) and GIT_ID.fullmatch(getattr(expected, field))
            for field in ("execution_commit", "execution_tree")
        )
        and len(expected.execution_commit) == len(expected.execution_tree),
        "invalid-source",
    )
    require(
        isinstance(expected.run_id, str)
        and RUN_ID.fullmatch(expected.run_id) is not None
        and type(expected.attempt) is int
        and 1 <= expected.attempt <= 2147483647,
        "invalid-run",
    )
    require(provenance.contract_profile == CONTRACT_PROFILE, "unsupported-receipt-contract")
    for field in ("execution_commit", "execution_tree", "run_id", "attempt"):
        actual, wanted = getattr(provenance, field), getattr(expected, field)
        require(type(actual) is type(wanted) and actual == wanted, "wrong-provenance-identity")
    require(
        isinstance(expected.selected, tuple) and len(expected.selected) <= 8, "invalid-selection"
    )
    for cell in expected.selected:
        require(isinstance(cell, Cell), "invalid-cell")
        cell.validate()
    require(len(set(expected.selected)) == len(expected.selected), "duplicate-selected-cell")
    require(
        isinstance(provenance.jobs, tuple) and len(provenance.jobs) == len(expected.selected),
        "incomplete-job-inventory",
    )
    jobs = {}
    ids = set()
    for job in provenance.jobs:
        require(
            isinstance(job, VerifiedJob) and job.cell in expected.selected and job.cell not in jobs,
            "unexpected-job",
        )
        require(
            type(job.job_id) is int and 1 <= job.job_id <= 2**53 - 1 and job.job_id not in ids,
            "invalid-job-identity",
        )
        require(
            type(job.attempt) is int and job.attempt == expected.attempt,
            "wrong-job-execution-attempt",
        )
        require(
            isinstance(job.outcome, str)
            and job.outcome in {"passed", "failed", "pending", "cancelled", "blocked"},
            "invalid-job-outcome",
        )
        require(
            isinstance(job.successful_steps, tuple)
            and len(job.successful_steps) <= 128
            and all(isinstance(step, str) and len(step) <= 256 for step in job.successful_steps)
            and len(set(job.successful_steps)) == len(job.successful_steps),
            "invalid-job-steps",
        )
        jobs[job.cell] = job
        ids.add(job.job_id)
    evidence = Evidence(artifact_root, provenance.files)
    from qualification.docker.scenario import QualificationError
    from scripts.test_planning_receipts import validate_cell

    results = []
    for cell in expected.selected:
        job = jobs[cell]
        evidence.accessed.clear()
        if job.outcome == "passed":
            require(
                set(REQUIRED_STEPS[cell.category]) <= set(job.successful_steps),
                "missing-successful-step",
            )
            try:
                validate_cell(evidence, expected, cell)
            except (
                AssertionError,
                KeyError,
                TypeError,
                ValueError,
                IndexError,
                OverflowError,
                QualificationError,
            ) as error:
                if isinstance(error, NormalizationError):
                    raise
                raise NormalizationError("invalid-custom-acceptance") from None
        used = [asdict(evidence.used[path]) for path in sorted(evidence.accessed)]
        results.append(
            {
                **asdict(cell),
                "outcome": job.outcome,
                "evidence_sha256": digest(
                    {
                        "contract_profile": CONTRACT_PROFILE,
                        "source": [expected.execution_commit, expected.execution_tree],
                        "run": [expected.run_id, expected.attempt],
                        "job": asdict(job),
                        "files": used,
                    }
                ),
            }
        )
    return {
        "test-results-version": 1,
        **{key: value for key, value in asdict(expected).items() if key != "selected"},
        "results": results,
    }
