"""Read-only same-attempt acquisition for the fixed manual shadow workflow.

Run this trusted adapter from the exact checked-out execution source. GitHub
metadata authenticates the run, jobs, upload intervals and original ZIP hashes;
artifact content never supplies its own authority or executable code. Selection
and its plan/policy identities are independently checked by the caller.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import struct
import subprocess
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from scripts.test_planning_results import (
    CONTRACT_PROFILE,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    REQUIRED_STEPS,
    Cell,
    ExpectedRun,
    NormalizationError,
    VerifiedFile,
    VerifiedJob,
    VerifiedRun,
    _json,
    canonical,
    sha,
)

REPOSITORY = "dariuszpanas/django-ray"
REPOSITORY_ID = 790856348
WORKFLOW = ".github/workflows/test-planning-shadow.yml"
WORKFLOW_NAME = "Test Planning Shadow"
WORKFLOW_ID = 388100602
PREFIX = "repos/" + REPOSITORY
MIB = 1024 * 1024
MAX_JSON = 2 * MIB
MAX_LOG = 20 * MIB
MAX_ZIP = 64 * MIB
MAX_EXPANDED = 128 * MIB
MAX_MEMBERS = 2048
MAX_ZIP_DIRECTORY = 4 * MIB
MAX_ATTEMPT = 1
UPLOAD_STEPS = {
    "application": "Upload application receipts and build identity",
    "native": "Retain native-stage evidence and explicit remaining acceptance",
    "latency": "Retain bounded qualification evidence",
    "cancellation": "Retain source and bounded cancellation receipts",
    "monitoring": "Retain bounded receipts and build identity",
}
JOB_NAMES = {
    "application": "application / Application Core (Linux)",
    "latency": "latency / latency",
    "cancellation": "cancellation / Cancellation Qualification",
    "monitoring": "monitoring / Monitoring Qualification (Linux)",
}
WORKFLOWS = ("application", "upgrade-native", "latency", "cancellation", "monitoring")
OUTCOMES = {
    "success": "passed",
    "failure": "failed",
    "timed_out": "failed",
    "startup_failure": "failed",
    "cancelled": "cancelled",
    "skipped": "blocked",
    "neutral": "blocked",
    "action_required": "blocked",
    "stale": "blocked",
}


class AcquisitionError(NormalizationError):
    """A fixed, safe-to-publish acquisition failure; never raw process output."""


class AcquisitionPending(AcquisitionError):  # noqa: N818 - pending observer control flow
    """Selected jobs or uploads are not ready; no output directory was created."""


DIAGNOSTIC_CODES = frozenset(
    """
    acquisition-io-failed acquisition-report-byte-budget acquisition-request-budget
    all-receipt-byte-budget ambiguous-artifact-name api-response-budget
    archive-total-byte-budget artifact-archive-digest-mismatch
    artifact-changed-during-acquisition artifact-log-outside-upload-window
    artifact-outside-upload-window artifact-upload-log-binding-missing
    conflicting-zip-topology copied-job-from-prior-attempt duplicate-api-identity
    duplicate-selected-cell duplicate-selected-job duplicate-zip-path
    github-cli-unavailable github-cli-probe-failed github-cli-probe-timeout
    github-cli-probe-byte-budget github-request-failed github-request-timeout
    github-request-unavailable github-response-byte-budget incomplete-api-inventory
    inconsistent-run-timestamps invalid-acquisition-data invalid-api-inventory
    invalid-api-timestamp invalid-artifact-digest-or-size invalid-artifact-identity
    invalid-artifact-zip invalid-expanded-receipt-size invalid-expected-run
    invalid-extracted-file-limit invalid-github-json invalid-github-object
    invalid-job-conclusion invalid-job-log invalid-job-status
    invalid-job-step-time-window invalid-job-steps invalid-job-time-window
    invalid-plan-identity invalid-response-limit invalid-run-creation-time
    invalid-run-identity invalid-selected-cell invalid-selected-cells
    invalid-shadow-run-status invalid-source-identity invalid-zip-directory
    invalid-zip-directory-count invalid-zip-directory-record job-changed-during-acquisition
    job-log-byte-budget linked-output-directory missing-artifact-receipts
    missing-artifact-source missing-passed-job-start missing-required-job-step
    missing-source-workflow-lineage missing-upload-step output-directory-not-fresh
    receipt-byte-budget receipt-total-byte-budget required-job-step-not-successful
    run-timestamps-changed-during-acquisition selected-artifact-not-published
    selected-job-incomplete selected-job-not-created shadow-observation-deadline
    fresh-shadow-run-required unsafe-zip-component unsafe-zip-member unsafe-zip-path
    unsupported-github-route wrong-artifact-source wrong-commit-tree wrong-job-detail
    wrong-job-lineage wrong-repository wrong-shadow-repository wrong-shadow-run
    wrong-shadow-source wrong-source-workflow-lineage wrong-workflow zip-byte-budget
    zip-directory-budget zip-directory-record-budget zip-expansion-budget
    """.split()
)


def diagnostic_code(error: BaseException) -> str:
    """Return only a reviewed fixed category, never exception rendering or payloads."""
    if isinstance(error, AcquisitionError) and len(error.args) == 1:
        code = error.args[0]
        if type(code) is str and len(code) <= 128 and code in DIAGNOSTIC_CODES:
            return code
    return "acquisition-failed"


class AcquisitionDiagnostics:
    """Bounded progress only; no API bodies, logs, endpoints or exception text."""

    def __init__(self) -> None:
        self._request_count = 0
        self._response_count = 0
        self._response_bytes = 0
        self._counters_saturated = False
        self._last_route_kind: str | None = None
        self._last_object_id: str | None = None
        self._last_response_bytes: int | None = None
        self._last_response_sha256: str | None = None

    def _count(self, field: str, amount: int) -> None:
        total = getattr(self, field) + amount
        maximum = 2**53 - 1
        if total > maximum:
            self._counters_saturated = True
        setattr(self, field, min(total, maximum))

    def request(self, endpoint: str) -> None:
        self._count("_request_count", 1)
        self._last_route_kind, self._last_object_id = _diagnostic_route(endpoint)
        self._last_response_bytes = self._last_response_sha256 = None

    def response(self, raw: bytes, maximum: int) -> None:
        # A malformed injected response remains the caller's validation failure;
        # diagnostics must neither retain it nor change acceptance behavior.
        if (
            type(raw) is not bytes
            or type(maximum) is not int
            or not 0 < maximum <= MAX_ZIP
            or len(raw) > maximum
        ):
            return
        self._count("_response_count", 1)
        self._count("_response_bytes", len(raw))
        self._last_response_bytes = len(raw)
        self._last_response_sha256 = hashlib.sha256(raw).hexdigest()

    def snapshot(self) -> dict:
        """Return a detached, resource-free report bounded below 4 KiB."""
        return {
            "request_count": self._request_count,
            "response_count": self._response_count,
            "response_bytes": self._response_bytes,
            "counters_saturated": self._counters_saturated,
            "last_route_kind": self._last_route_kind,
            "last_object_id": self._last_object_id,
            "last_response_bytes": self._last_response_bytes,
            "last_response_sha256": self._last_response_sha256,
        }


def require(condition: bool, code: str) -> None:
    if not condition:
        raise AcquisitionError(code)


def _integer(value: object, maximum: int = 2**53 - 1) -> bool:
    return type(value) is int and 0 < value <= maximum


def _timestamp(value: object) -> datetime:
    require(
        type(value) is str
        and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?Z", value) is not None,
        "invalid-api-timestamp",
    )
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AcquisitionError("invalid-api-timestamp") from None


def _same(actual: object, expected: object, code: str) -> None:
    require(canonical(actual) == canonical(expected), code)


def _endpoint(value: str) -> bool:
    return (
        re.fullmatch(
            re.escape(PREFIX)
            + r"(?:|/git/commits/[0-9a-f]{40}|/actions/workflows/test-planning-shadow\.yml"
            + r"|/actions/runs/[1-9][0-9]{0,15}(?:/attempts/[1-9][0-9]?(?:/jobs\?per_page=100&page=1)?|/artifacts\?per_page=100&page=1)?"
            + r"|/actions/jobs/[1-9][0-9]{0,15}(?:/logs)?|/actions/artifacts/[1-9][0-9]{0,15}(?:/zip)?)",
            value,
        )
        is not None
    )


def _diagnostic_route(endpoint: str) -> tuple[str, str | None]:
    if type(endpoint) is not str or len(endpoint) > 256 or not _endpoint(endpoint):
        return "unsupported", None
    route = endpoint.removeprefix(PREFIX)
    if not route:
        return "repository", None
    if route.startswith("/git/commits/"):
        return "commit", None
    if route.startswith("/actions/workflows/"):
        return "workflow", None
    parts = route.split("/")
    object_id = parts[3] if _integer(int(parts[3])) else None
    if parts[2] == "runs":
        if len(parts) == 7:
            return "run-jobs", object_id
        if len(parts) == 6:
            return "run-attempt", object_id
        return ("run-artifacts" if len(parts) == 5 else "run"), object_id
    if parts[2] == "jobs":
        return ("job-log" if len(parts) == 5 else "job"), object_id
    return ("artifact-archive" if len(parts) == 5 else "artifact"), object_id


class Github:
    """Use authenticated gh GETs on fixed routes; bound output and wall time."""

    def __init__(self) -> None:
        executable = shutil.which("gh")
        require(executable is not None, "github-cli-unavailable")
        self.executable = str(Path(executable).resolve(strict=True))
        self.deadline = time.monotonic() + 240
        self.calls = 0
        self._log_arguments: tuple[str, ...] | None = None
        self._probe_error: str | None = None

    def get(self, endpoint: str, maximum: int) -> bytes:
        require(_endpoint(endpoint), "unsupported-github-route")
        require(type(maximum) is int and 0 < maximum <= MAX_ZIP, "invalid-response-limit")
        require(self.calls < 100 and time.monotonic() < self.deadline, "acquisition-request-budget")
        options = ()
        if _diagnostic_route(endpoint)[0] == "job-log":
            if self._probe_error is not None:
                raise AcquisitionError(self._probe_error)
            if self._log_arguments is None:
                try:
                    help_text = self._capture(["api", "--help"], 65536, 5, probe=True)
                    require(
                        re.search(rb"(?m)^USAGE\r?$", help_text) is not None
                        and re.search(rb"(?m)^[ \t]+gh api <endpoint> \[flags\]\r?$", help_text)
                        is not None,
                        "github-cli-probe-failed",
                    )
                    self._log_arguments = (
                        ("--allow-escape-sequences",)
                        if re.search(
                            rb"(?m)^[ \t]+--allow-escape-sequences(?:[ \t]+|\r?$)", help_text
                        )
                        is not None
                        else ()
                    )
                except AcquisitionError as error:
                    self._probe_error = diagnostic_code(error)
                    raise
            options = self._log_arguments
        require(time.monotonic() < self.deadline, "acquisition-request-budget")
        self.calls += 1
        return self._capture(
            ["api", "--hostname", "github.com", "--method", "GET", *options, endpoint], maximum, 30
        )

    def _capture(
        self, arguments: list[str], maximum: int, seconds: int, *, probe: bool = False
    ) -> bytes:
        """Keep CLI capability text and raw evidence inside bounded private pipes."""
        failure_code = "github-cli-probe-failed" if probe else "github-request-failed"
        timeout_code = "github-cli-probe-timeout" if probe else "github-request-timeout"
        byte_code = "github-cli-probe-byte-budget" if probe else "github-response-byte-budget"
        deadline = min(self.deadline, time.monotonic() + seconds)
        require(time.monotonic() < deadline, timeout_code)
        environment = os.environ.copy()
        for name in ("GH_DEBUG", "GH_FORCE_TTY", "CLICOLOR_FORCE", "FORCE_COLOR"):
            environment.pop(name, None)
        # gh recognizes "cat" as disabling its pager without spawning a process.
        environment.update(
            GH_PROMPT_DISABLED="1",
            GH_NO_UPDATE_NOTIFIER="1",
            GH_PAGER="cat",
            PAGER="cat",
            NO_COLOR="1",
            CLICOLOR="0",
        )
        try:
            process = subprocess.Popen(
                [self.executable, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                shell=False,
            )
        except OSError:
            raise AcquisitionError(
                failure_code if probe else "github-request-unavailable"
            ) from None
        output = bytearray()
        failures = []

        def consume():
            try:
                while chunk := process.stdout.read(65536):
                    if len(output) + len(chunk) > maximum:
                        raise AcquisitionError(byte_code)
                    output.extend(chunk)
            except Exception as error:
                failures.append(
                    byte_code
                    if type(error) is AcquisitionError and error.args == (byte_code,)
                    else failure_code
                )
                process.kill()

        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        try:
            reader.join(timeout=max(0, deadline - time.monotonic()))
            require(not reader.is_alive(), timeout_code)
            if failures:
                raise AcquisitionError(failures[0])
            require(
                process.wait(timeout=max(0, min(5, deadline - time.monotonic()))) == 0, failure_code
            )
            require(time.monotonic() < deadline, timeout_code)
            return bytes(output)
        except subprocess.TimeoutExpired:
            raise AcquisitionError(timeout_code) from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdout.close()

    def json(self, endpoint: str) -> dict:
        """Read one bounded object on the same fixed GitHub GET route boundary."""
        raw = self.get(endpoint, MAX_JSON)
        try:
            value = _json(raw)
        except NormalizationError:
            raise AcquisitionError("invalid-github-json") from None
        require(type(value) is dict, "invalid-github-object")
        return value


@dataclass(frozen=True)
class Acquisition:
    root: Path
    provenance: VerifiedRun
    evidence: dict


def artifact_name(cell: Cell, run_id: str) -> str:
    return {
        "application": f"application-core-{run_id}",
        "native": f"upgrade-native-qualification-{cell.variant}",
        "latency": "job-latency-qualification",
        "cancellation": "cancellation-qualification",
        "monitoring": f"monitoring-qualification-{run_id}",
    }[cell.category]


def receipt_paths(cell: Cell) -> frozenset[str]:
    """Only these ordinary data files may enter the normalizer's directory."""
    if cell.category == "application":
        return frozenset(
            {
                "application-build/" + name
                for name in (
                    "images.json",
                    "resource-profile.txt",
                    "source.sha256",
                    "source.txt",
                    "validation-intent.txt",
                )
            }
            | {
                "application-evidence/" + name + ".json"
                for name in ("producers", "reporting", "setup", "summary")
            }
            | {
                f"application-evidence/{generation}-{layer}.json"
                for generation in ("before", "after")
                for layer in (
                    "core",
                    "first",
                    "nodes",
                    "protected-logs",
                    "protocol",
                    "rq2",
                    "showcase",
                    "workflows",
                )
            }
        )
    if cell.category == "monitoring":
        return frozenset(
            {
                "monitoring-build/" + name
                for name in ("ancestry.json", "images.json", "source.sha256", "source.txt")
            }
            | {
                "monitoring-evidence/" + name
                for name in ("infrastructure-cleanup.json", "summary.json")
            }
        )
    prefix = {"native": "upgrade", "latency": "latency", "cancellation": "cancellation"}[
        cell.category
    ]
    paths = {
        f"{prefix}-source.txt",
        f"{prefix}-image.txt",
        f"{prefix}-receipts/execution-manifest.json",
    }
    if cell.category == "native":
        paths.add("upgrade-baseline-sha256.txt")
    return frozenset(paths)


def validate_run(value: dict, expected: ExpectedRun) -> None:
    for key, wanted in {
        "id": int(expected.run_id),
        "run_attempt": expected.attempt,
        "workflow_id": WORKFLOW_ID,
        "path": WORKFLOW,
        "name": WORKFLOW_NAME,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": expected.execution_commit,
    }.items():
        _same(value.get(key), wanted, "wrong-shadow-run")
    for key in ("repository", "head_repository"):
        repository = value.get(key)
        require(
            type(repository) is dict
            and repository.get("id") == REPOSITORY_ID
            and repository.get("full_name") == REPOSITORY,
            "wrong-shadow-repository",
        )
    head = value.get("head_commit")
    require(
        type(head) is dict
        and head.get("id") == expected.execution_commit
        and head.get("tree_id") == expected.execution_tree,
        "wrong-shadow-source",
    )
    require(
        value.get("status") in {"queued", "in_progress", "completed"}, "invalid-shadow-run-status"
    )
    references = value.get("referenced_workflows")
    wanted = {
        f"{REPOSITORY}/.github/workflows/{name}-qualification.yml@{expected.execution_commit}"
        for name in WORKFLOWS
    }
    require(
        type(references) is list
        and len(references) == 5
        and all(type(item) is dict for item in references),
        "missing-source-workflow-lineage",
    )
    references = cast(list[dict], references)
    require(
        {item.get("path") for item in references} == wanted
        and all(
            item.get("sha") == expected.execution_commit and item.get("ref") == "refs/heads/main"
            for item in references
        ),
        "wrong-source-workflow-lineage",
    )
    _timestamp(value.get("run_started_at"))
    require(
        _timestamp(value.get("created_at")) <= _timestamp(value["run_started_at"]),
        "invalid-run-creation-time",
    )


def _items(value: dict, key: str) -> list[dict]:
    items = value.get(key)
    count = value.get("total_count")
    require(
        type(items) is list and type(count) is int and count == len(items) and count < 100,
        "incomplete-api-inventory",
    )
    items = cast(list[dict], items)
    require(
        all(type(item) is dict and _integer(item.get("id")) for item in items),
        "invalid-api-inventory",
    )
    require(len({item["id"] for item in items}) == count, "duplicate-api-identity")
    return items


def _job(
    value: dict, cell: Cell, expected: ExpectedRun, run: dict, previous: set[int]
) -> dict | None:
    require(
        _integer(value.get("id")) and value["id"] not in previous, "copied-job-from-prior-attempt"
    )
    name = (
        f"native / Upgrade Native ({cell.variant})"
        if cell.category == "native"
        else JOB_NAMES[cell.category]
    )
    for key, wanted in {
        "name": name,
        "run_id": int(expected.run_id),
        "run_attempt": expected.attempt,
        "head_sha": expected.execution_commit,
        "head_branch": "main",
        "workflow_name": WORKFLOW_NAME,
    }.items():
        _same(value.get(key), wanted, "wrong-job-lineage")
    if value.get("status") != "completed":
        require(
            value.get("status") in {"queued", "in_progress", "waiting", "pending"},
            "invalid-job-status",
        )
        raise AcquisitionPending("selected-job-incomplete")
    require(value.get("conclusion") in OUTCOMES, "invalid-job-conclusion")
    end = _timestamp(value.get("completed_at"))
    start = _timestamp(value["started_at"]) if value.get("started_at") is not None else None
    require(
        _timestamp(run["run_started_at"]) <= (start or end) <= end,
        "invalid-job-time-window",
    )
    steps = value.get("steps")
    require(
        type(steps) is list and len(steps) <= 100 and all(type(step) is dict for step in steps),
        "invalid-job-steps",
    )
    steps = cast(list[dict], steps)
    # An authenticated nonpassing terminal job is useful failure evidence. Its
    # partial steps and uploaded artifacts never authorize a passed receipt.
    if value["conclusion"] != "success":
        return None
    if start is None:
        raise AcquisitionError("missing-passed-job-start")
    for required in (*REQUIRED_STEPS[cell.category], UPLOAD_STEPS[cell.category]):
        matching = [step for step in steps if step.get("name") == required]
        require(len(matching) == 1, "missing-required-job-step")
        step = matching[0]
        require(
            step.get("status") == "completed" and step.get("conclusion") == "success",
            "required-job-step-not-successful",
        )
        require(
            start
            <= _timestamp(step.get("started_at"))
            <= _timestamp(step.get("completed_at"))
            <= end,
            "invalid-job-step-time-window",
        )
    return next(step for step in steps if step.get("name") == UPLOAD_STEPS[cell.category])


def validate_artifact_metadata(value: dict, expected: ExpectedRun, upload: dict, name: str) -> None:
    require(
        _integer(value.get("id")) and value.get("name") == name and value.get("expired") is False,
        "invalid-artifact-identity",
    )
    require(
        _integer(value.get("size_in_bytes"), MAX_ZIP)
        and type(value.get("digest")) is str
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value["digest"]) is not None,
        "invalid-artifact-digest-or-size",
    )
    identity = value.get("workflow_run")
    require(type(identity) is dict, "missing-artifact-source")
    for key, wanted in {
        "id": int(expected.run_id),
        "repository_id": REPOSITORY_ID,
        "head_repository_id": REPOSITORY_ID,
        "head_sha": expected.execution_commit,
        "head_branch": "main",
    }.items():
        _same(identity.get(key), wanted, "wrong-artifact-source")
    require(
        _timestamp(upload["started_at"])
        <= _timestamp(value.get("created_at"))
        <= _timestamp(value.get("updated_at"))
        <= _timestamp(upload["completed_at"]),
        "artifact-outside-upload-window",
    )


def validate_upload_log(raw: bytes, artifact: dict, upload: dict) -> None:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        raise AcquisitionError("invalid-job-log") from None
    identity = f"Artifact {artifact['name']} has been successfully uploaded! Final size is {artifact['size_in_bytes']} bytes. Artifact ID is {artifact['id']}"
    digest = f"SHA256 digest of uploaded artifact is {artifact['digest'][7:]}"
    found = []
    for line in lines:
        if len(line) > 4096:
            continue
        stamp, separator, message = line.partition(" ")
        if separator and message in (identity, digest):
            require(
                _timestamp(upload["started_at"])
                <= _timestamp(stamp)
                < _timestamp(upload["completed_at"]) + timedelta(seconds=1),
                "artifact-log-outside-upload-window",
            )
            found.append(message)
    require(sorted(found) == sorted((identity, digest)), "artifact-upload-log-binding-missing")


def _safe_member(info: zipfile.ZipInfo) -> tuple[str, bool]:
    name = info.filename
    directory = info.is_dir()
    path = name[:-1] if directory else name
    require(
        info.orig_filename == name
        and 0 < len(path) <= 1024
        and re.fullmatch(r"[A-Za-z0-9_./-]+", path) is not None,
        "unsafe-zip-path",
    )
    parts = path.split("/")
    require(
        len(parts) <= 64
        and all(
            part not in {"", ".", ".."}
            and len(part) <= 255
            and not part.endswith(".")
            and re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) is None
            for part in parts
        ),
        "unsafe-zip-component",
    )
    kind = stat.S_IFMT(info.external_attr >> 16)
    require(
        kind in (0, stat.S_IFDIR if directory else stat.S_IFREG)
        and not info.flag_bits & 1
        and info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
        and 0 <= info.file_size <= 32 * MIB
        and 0 <= info.compress_size <= MAX_ZIP
        and (not directory or info.file_size == 0),
        "unsafe-zip-member",
    )
    return path, directory


def _zip_directory(raw: bytes) -> None:
    """Bound ordinary ZIP metadata before zipfile allocates its member objects.

    The fixed uploader produces single-disk archives. ZIP64, trailing overlays
    and extended directory records are unsupported rather than inferred.
    """
    offset = raw.rfind(b"PK\x05\x06", max(0, len(raw) - 65557))
    require(offset >= 0 and offset + 22 <= len(raw), "invalid-zip-directory")
    disk, directory_disk, disk_count, count, size, start, comment = struct.unpack_from(
        "<HHHHIIH", raw, offset + 4
    )
    require(
        disk == directory_disk == 0
        and disk_count == count
        and 1 <= count <= MAX_MEMBERS
        and size <= MAX_ZIP_DIRECTORY
        and start + size == offset
        and comment <= 4096
        and offset + 22 + comment == len(raw),
        "zip-directory-budget",
    )
    cursor, seen = start, 0
    while cursor < offset:
        require(
            cursor + 46 <= offset and raw[cursor : cursor + 4] == b"PK\x01\x02",
            "invalid-zip-directory-record",
        )
        name, extra, note, member_disk = struct.unpack_from("<HHHH", raw, cursor + 28)
        seen += 1
        require(
            seen <= count
            and 1 <= name <= 1024
            and extra <= 4096
            and note <= 4096
            and member_disk == 0,
            "zip-directory-record-budget",
        )
        cursor += 46 + name + extra + note
    require(cursor == offset and seen == count, "invalid-zip-directory-count")


def extract_receipts(
    raw: bytes, required: frozenset[str], *, max_file_bytes: int = MAX_FILE_BYTES
) -> dict[str, bytes]:
    """Validate the full ZIP inventory; decompress only fixed receipt data files."""
    require(
        type(max_file_bytes) is int and 0 < max_file_bytes <= 4 * MIB,
        "invalid-extracted-file-limit",
    )
    require(len(raw) <= MAX_ZIP, "zip-byte-budget")
    try:
        _zip_directory(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            require(
                0 < len(infos) <= MAX_MEMBERS
                and sum(info.file_size for info in infos) <= MAX_EXPANDED,
                "zip-expansion-budget",
            )
            members = {}
            topology = {}
            for info in infos:
                path, directory = _safe_member(info)
                key = path.casefold()
                require(key not in topology, "duplicate-zip-path")
                topology[key] = directory
                if not directory:
                    members[path] = info
            for path in topology:
                parts = path.split("/")
                require(
                    all(
                        topology.get("/".join(parts[:index]), True)
                        for index in range(1, len(parts))
                    ),
                    "conflicting-zip-topology",
                )
            require(required <= set(members), "missing-artifact-receipts")
            require(
                sum(members[path].file_size for path in required) <= MAX_TOTAL_BYTES,
                "receipt-total-byte-budget",
            )
            result = {}
            for path in sorted(required):
                info = members[path]
                require(info.file_size <= max_file_bytes, "receipt-byte-budget")
                with archive.open(info) as stream:
                    data = stream.read(max_file_bytes + 1)
                require(len(data) == info.file_size, "invalid-expanded-receipt-size")
                result[path] = data
            require(sum(map(len, result.values())) <= MAX_TOTAL_BYTES, "receipt-total-byte-budget")
            return result
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        raise AcquisitionError("invalid-artifact-zip") from None


def _no_links(path: Path) -> None:
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            attributes = item.lstat()
            require(
                not stat.S_ISLNK(attributes.st_mode)
                and not getattr(attributes, "st_file_attributes", 0) & 0x400,
                "linked-output-directory",
            )


def _acquire_receipts(
    expected: ExpectedRun,
    destination: Path,
    *,
    github: Github | None = None,
    diagnostics: AcquisitionDiagnostics | None = None,
) -> Acquisition:
    """Acquire one explicit selected-cell observation, without polling or running tests."""
    require(isinstance(expected, ExpectedRun), "invalid-expected-run")
    require(
        type(expected.run_id) is str
        and re.fullmatch(r"[1-9][0-9]{0,15}", expected.run_id) is not None
        and _integer(expected.attempt, MAX_ATTEMPT),
        "invalid-run-identity",
    )
    require(
        all(
            type(value) is str and re.fullmatch(r"[0-9a-f]{40}", value) is not None
            for value in (expected.execution_commit, expected.execution_tree)
        ),
        "invalid-source-identity",
    )
    require(
        all(
            sha(value)
            for value in (expected.plan_digest, expected.policy_sha256, expected.bindings_sha256)
        ),
        "invalid-plan-identity",
    )
    require(
        type(expected.selected) is tuple and len(expected.selected) <= 8, "invalid-selected-cells"
    )
    for cell in expected.selected:
        require(isinstance(cell, Cell), "invalid-selected-cell")
        cell.validate()
    require(len(set(expected.selected)) == len(expected.selected), "duplicate-selected-cell")
    client = github if github is not None else Github()
    snapshots = {}

    def request(endpoint: str, maximum: int) -> bytes:
        if diagnostics is not None:
            diagnostics.request(endpoint)
        raw = client.get(endpoint, maximum)
        if diagnostics is not None:
            diagnostics.response(raw, maximum)
        return raw

    def read(endpoint: str, label: str) -> dict:
        raw = request(endpoint, MAX_JSON)
        require(type(raw) is bytes and len(raw) <= MAX_JSON, "api-response-budget")
        try:
            value = _json(raw)
        except NormalizationError:
            raise AcquisitionError("invalid-github-json") from None
        require(type(value) is dict, "invalid-github-object")
        snapshots[label] = raw
        return value

    run_route = f"{PREFIX}/actions/runs/{expected.run_id}"
    jobs = _items(
        read(f"{run_route}/attempts/{expected.attempt}/jobs?per_page=100&page=1", "jobs"), "jobs"
    )
    # Pending polls make exactly one small API request; no partial download or
    # repeated completed-job log fetch while other selected work is unfinished.
    for cell in expected.selected:
        name = (
            f"native / Upgrade Native ({cell.variant})"
            if cell.category == "native"
            else JOB_NAMES[cell.category]
        )
        matches = [job for job in jobs if job.get("name") == name]
        if not matches:
            raise AcquisitionPending("selected-job-not-created")
        require(len(matches) == 1, "duplicate-selected-job")
        job = matches[0]
        for key, wanted in {
            "run_id": int(expected.run_id),
            "run_attempt": 1,
            "head_sha": expected.execution_commit,
        }.items():
            _same(job.get(key), wanted, "wrong-job-lineage")
        if job.get("status") != "completed":
            require(
                job.get("status") in {"queued", "in_progress", "waiting", "pending"},
                "invalid-job-status",
            )
            raise AcquisitionPending("selected-job-incomplete")
        require(job.get("conclusion") in OUTCOMES, "invalid-job-conclusion")
    repository = read(PREFIX, "repository")
    _same(
        (repository.get("id"), repository.get("full_name")),
        (REPOSITORY_ID, REPOSITORY),
        "wrong-repository",
    )
    workflow = read(PREFIX + "/actions/workflows/test-planning-shadow.yml", "workflow")
    _same(
        (workflow.get("id"), workflow.get("path"), workflow.get("name")),
        (WORKFLOW_ID, WORKFLOW, WORKFLOW_NAME),
        "wrong-workflow",
    )
    commit = read(PREFIX + "/git/commits/" + expected.execution_commit, "commit")
    _same(
        (commit.get("sha"), commit.get("tree", {}).get("sha")),
        (expected.execution_commit, expected.execution_tree),
        "wrong-commit-tree",
    )
    run = read(run_route, "run-before")
    validate_run(run, expected)
    attempt = read(f"{run_route}/attempts/{expected.attempt}", "attempt")
    validate_run(attempt, expected)
    _same(
        [attempt[key] for key in ("created_at", "run_started_at")],
        [run[key] for key in ("created_at", "run_started_at")],
        "inconsistent-run-timestamps",
    )
    previous = set()
    artifacts = _items(read(run_route + "/artifacts?per_page=100&page=1", "artifacts"), "artifacts")
    selected = []
    for cell in expected.selected:
        name = (
            f"native / Upgrade Native ({cell.variant})"
            if cell.category == "native"
            else JOB_NAMES[cell.category]
        )
        matches = [job for job in jobs if job.get("name") == name]
        if not matches:
            raise AcquisitionPending("selected-job-not-created")
        require(len(matches) == 1, "duplicate-selected-job")
        job = read(f"{PREFIX}/actions/jobs/{matches[0]['id']}", f"job-{matches[0]['id']}")
        require(job.get("id") == matches[0]["id"], "wrong-job-detail")
        upload = _job(job, cell, expected, run, previous)
        if upload is None:
            selected.append((cell, job, None, None))
            continue
        artifact = artifact_name(cell, expected.run_id)
        matches = [item for item in artifacts if item.get("name") == artifact]
        if not matches:
            raise AcquisitionPending("selected-artifact-not-published")
        require(len(matches) == 1, "ambiguous-artifact-name")
        validate_artifact_metadata(matches[0], expected, upload, artifact)
        selected.append((cell, job, upload, matches[0]))
    require(
        sum(item[3]["size_in_bytes"] for item in selected if item[3] is not None) <= 256 * MIB,
        "archive-total-byte-budget",
    )
    destination = Path(destination).absolute()
    _no_links(destination)
    require(not destination.exists() and destination.parent.is_dir(), "output-directory-not-fresh")
    destination.mkdir()
    for name in ("archives", "logs", "api", "receipts"):
        (destination / name).mkdir()
    files, verified_jobs, records = [], [], []
    total = 0
    for cell, job, upload, artifact in selected:
        log_sha256 = None
        if artifact is not None:
            require(upload is not None, "missing-upload-step")
            logs = request(f"{PREFIX}/actions/jobs/{job['id']}/logs", MAX_LOG)
            require(type(logs) is bytes and len(logs) <= MAX_LOG, "job-log-byte-budget")
            validate_upload_log(logs, artifact, upload)
            (destination / "logs" / f"{job['id']}.log").write_bytes(logs)
            log_sha256 = hashlib.sha256(logs).hexdigest()
            raw = request(f"{PREFIX}/actions/artifacts/{artifact['id']}/zip", MAX_ZIP)
            require(
                type(raw) is bytes
                and len(raw) == artifact["size_in_bytes"]
                and "sha256:" + hashlib.sha256(raw).hexdigest() == artifact["digest"],
                "artifact-archive-digest-mismatch",
            )
            (destination / "archives" / f"{artifact['id']}.zip").write_bytes(raw)
            extracted = extract_receipts(raw, receipt_paths(cell))
            for name, data in extracted.items():
                relative = artifact["name"] + "/" + name
                target = destination / "receipts" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    output.write(data)
                total += len(data)
                require(total <= MAX_TOTAL_BYTES, "all-receipt-byte-budget")
                files.append(VerifiedFile(relative, len(data), hashlib.sha256(data).hexdigest()))
            _same(
                read(
                    f"{PREFIX}/actions/artifacts/{artifact['id']}",
                    f"artifact-{artifact['id']}-after",
                ),
                artifact,
                "artifact-changed-during-acquisition",
            )
        detail = read(f"{PREFIX}/actions/jobs/{job['id']}", f"job-{job['id']}-after")
        _same(detail, job, "job-changed-during-acquisition")
        outcome = OUTCOMES[job["conclusion"]]
        verified_jobs.append(
            VerifiedJob(
                cell,
                job["id"],
                expected.attempt,
                outcome,
                (*REQUIRED_STEPS[cell.category], UPLOAD_STEPS[cell.category])
                if outcome == "passed"
                else (),
            )
        )
        records.append(
            {
                "cell": asdict(cell),
                "job_id": job["id"],
                "actual_attempt": expected.attempt,
                "outcome": outcome,
                "github_conclusion": job["conclusion"],
                "started_at": job.get("started_at"),
                "completed_at": job["completed_at"],
                "duration_seconds": (
                    _timestamp(job["completed_at"]) - _timestamp(job["started_at"])
                ).total_seconds()
                if job.get("started_at") is not None
                else None,
                "upload_started_at": upload["started_at"] if upload is not None else None,
                "upload_completed_at": upload["completed_at"] if upload is not None else None,
                "artifact": artifact,
                "log_sha256": log_sha256,
            }
        )
    after = read(run_route, "run-after")
    validate_run(after, expected)
    _same(
        [after[key] for key in ("created_at", "run_started_at")],
        [run[key] for key in ("created_at", "run_started_at")],
        "run-timestamps-changed-during-acquisition",
    )
    api_index = []
    for label, raw in snapshots.items():
        (destination / "api" / (label + ".json")).write_bytes(raw)
        api_index.append(
            {
                "path": "api/" + label + ".json",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    provenance = VerifiedRun(
        expected.execution_commit,
        expected.execution_tree,
        expected.run_id,
        expected.attempt,
        CONTRACT_PROFILE,
        tuple(verified_jobs),
        tuple(sorted(files, key=lambda item: item.path)),
    )
    evidence = {
        "acquisition-version": 1,
        "repository": REPOSITORY,
        "workflow": WORKFLOW,
        "source": expected.execution_commit,
        "tree": expected.execution_tree,
        "run_id": expected.run_id,
        "attempt": expected.attempt,
        "run_started_at": run["run_started_at"],
        "workflow_created_at": run["created_at"],
        "contract_profile": CONTRACT_PROFILE,
        "prior_attempt_job_ids": sorted(previous),
        "selected_jobs": records,
        "artifacts": [
            {**record["artifact"], "sha256": record["artifact"]["digest"][7:]}
            for record in records
            if record["artifact"] is not None
        ],
        "files": [asdict(item) for item in provenance.files],
        "api": api_index,
    }
    raw = canonical(evidence) + b"\n"
    require(len(raw) <= MAX_JSON, "acquisition-report-byte-budget")
    (destination / "acquisition.json").write_bytes(raw)
    return Acquisition(destination / "receipts", provenance, evidence)


def acquire_receipts(
    expected: ExpectedRun,
    destination: Path,
    *,
    github: Github | None = None,
    diagnostics: AcquisitionDiagnostics | None = None,
) -> Acquisition:
    """Bound malformed remote data to fixed errors, including first-attempt-only use."""
    try:
        return _acquire_receipts(expected, destination, github=github, diagnostics=diagnostics)
    except AcquisitionError:
        raise
    except (NormalizationError, KeyError, TypeError, AttributeError, IndexError, ValueError):
        raise AcquisitionError("invalid-acquisition-data") from None
    except OSError:
        raise AcquisitionError("acquisition-io-failed") from None
