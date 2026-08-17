"""Fail-closed GitHub pull-request review policy checks.

This script is intended for a trusted ``pull_request_target`` workflow.  It
uses only GitHub REST responses and never checks out or executes pull-request
code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

MAINTAINER_USER_ID = 15_094_983
CODEX_CONNECTOR_USER_ID = 199_175_422
GITHUB_ACTIONS_USER_ID = 41_898_282
MAINTAINER_LOGIN = "dariuszpanas"
CODEX_CONNECTOR_LOGIN = "chatgpt-codex-connector[bot]"
GITHUB_ACTIONS_LOGIN = "github-actions[bot]"

API_VERSION = "2022-11-28"
MAX_API_PAGES = 10
MAX_API_RECORDS = 1_000
MAX_API_REQUESTS = 300
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_GITHUB_ID = 9_223_372_036_854_775_807
MAX_BASE_REF_BYTES = 255
MAX_EVENT_PAYLOAD_BYTES = 32 * 1024 * 1024
MAX_PULL_REQUEST_TITLE_BYTES = 1_024
MAX_PULL_REQUEST_BODY_BYTES = 256 * 1_024
MAX_RUN_ATTEMPT = 10_000
MAX_POLL_TIMEOUT_SECONDS = 1_800.0
MAX_POLL_INTERVAL_SECONDS = 60.0
MAX_REQUEST_TIMEOUT_SECONDS = 30.0
MAX_REQUEST_COMMENT_CANDIDATES = 20
MAX_WORKFLOW_RUN_RECORDS = 100
REACTION_PAGE_SIZE = 100

CODEX_WORKFLOW_FILE = "codex-review.yml"
CODEX_WORKFLOW_PATH = f".github/workflows/{CODEX_WORKFLOW_FILE}"

REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
METADATA_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
ELIGIBLE_CODEX_ACTIONS = frozenset(
    {"edited", "opened", "ready_for_review", "synchronize", "reopened"}
)
TERMINAL_CODEX_ACTIONS = frozenset({"closed", "converted_to_draft"})
CODEX_ACTIONS = tuple(sorted(ELIGIBLE_CODEX_ACTIONS | TERMINAL_CODEX_ACTIONS))
MEANINGFUL_MAINTAINER_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})
LIFECYCLE_EVENTS = frozenset({"closed", "convert_to_draft", "ready_for_review", "reopened"})
LIFECYCLE_ACTION_EVENTS = {
    "closed": "closed",
    "converted_to_draft": "convert_to_draft",
    "ready_for_review": "ready_for_review",
    "reopened": "reopened",
}


class ReviewPolicyError(RuntimeError):
    """Raised when evidence is missing, stale, malformed, or unsafe."""


class RestApi(Protocol):
    """The small GitHub REST surface used by the policies."""

    def get(self, path: str) -> object: ...

    def post(self, path: str, payload: dict[str, object]) -> object: ...

    def paginate(self, path: str) -> list[dict[str, Any]]: ...


class GitHubRestApi:
    """Authenticated GitHub REST client with bounded responses and pagination."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        request_timeout: float = 15.0,
    ) -> None:
        if not token:
            raise ReviewPolicyError("GITHUB_TOKEN is required")
        parsed_url = urllib.parse.urlsplit(base_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ReviewPolicyError("GITHUB_API_URL must be an HTTPS origin or base path")
        if not 1.0 <= request_timeout <= MAX_REQUEST_TIMEOUT_SECONDS:
            raise ReviewPolicyError(
                f"request timeout must be between 1 and {MAX_REQUEST_TIMEOUT_SECONDS:g} seconds"
            )
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._request_timeout = request_timeout
        self._request_count = 0

    def _consume_request_budget(self) -> None:
        if self._request_count >= MAX_API_REQUESTS:
            raise ReviewPolicyError("GitHub API request budget exhausted")
        self._request_count += 1

    def _request_json(
        self,
        path: str,
        *,
        method: str,
        payload: dict[str, object] | None = None,
    ) -> object:
        """Send one bounded GitHub REST request and decode its response."""
        if not path.startswith("/") or "\\" in path or "\n" in path or "\r" in path:
            raise ReviewPolicyError("GitHub API path is invalid")
        if method not in {"GET", "POST"} or (method == "GET") != (payload is None):
            raise ReviewPolicyError("GitHub API request method is invalid")
        request_body = None
        if payload is not None:
            request_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if len(request_body) > 4_096:
                raise ReviewPolicyError("GitHub API request exceeded the byte limit")
        self._consume_request_budget()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "django-ray-review-policy",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if request_body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=request_body,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - URL is validated and path is internal.
                request, timeout=self._request_timeout
            ) as response:
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise ReviewPolicyError(f"GitHub API {method} failed with HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            detail = " ".join(str(getattr(error, "reason", error)).split())[:300]
            raise ReviewPolicyError(f"GitHub API {method} failed: {detail}") from error
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise ReviewPolicyError("GitHub API response exceeded the byte limit")
        try:
            return json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReviewPolicyError("GitHub API returned invalid JSON") from error

    def get(self, path: str) -> object:
        """Read and decode one bounded GitHub REST response."""
        return self._request_json(path, method="GET")

    def post(self, path: str, payload: dict[str, object]) -> object:
        """Create one bounded GitHub REST resource."""
        return self._request_json(path, method="POST", payload=payload)

    def paginate(self, path: str) -> list[dict[str, Any]]:
        """Read at most ten 100-record pages from one list endpoint."""
        separator = "&" if "?" in path else "?"
        records: list[dict[str, Any]] = []
        for page in range(1, MAX_API_PAGES + 1):
            response = self.get(f"{path}{separator}per_page=100&page={page}")
            if not isinstance(response, list) or not all(
                isinstance(item, dict) for item in response
            ):
                raise ReviewPolicyError("GitHub API pagination returned an invalid page")
            page_records = cast(list[dict[str, Any]], response)
            records.extend(page_records)
            if len(records) > MAX_API_RECORDS:
                raise ReviewPolicyError("GitHub API pagination exceeded the record limit")
            if len(page_records) < 100:
                return records
        raise ReviewPolicyError("GitHub API pagination exceeded the page limit")


@dataclass(frozen=True)
class PullRequest:
    """Security-relevant live pull-request fields."""

    number: int
    head_sha: str
    base_ref: str
    base_sha: str
    author_id: int
    draft: bool
    state: str
    updated_at: datetime
    metadata_digest: str


@dataclass(frozen=True)
class WorkflowRun:
    """Trusted identity of the executing Codex review workflow run."""

    run_id: int
    run_number: int
    run_attempt: int
    created_at: datetime
    run_started_at: datetime


def _record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewPolicyError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_GITHUB_ID:
        raise ReviewPolicyError(f"{label} must be a bounded positive integer")
    return value


def _repository(value: str) -> str:
    if not REPOSITORY_RE.fullmatch(value):
        raise ReviewPolicyError("repository must be an owner/name pair")
    return value


def _commit_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ReviewPolicyError(f"{label} must be a full lowercase commit SHA")
    return value


def _base_ref(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ReviewPolicyError("expected base ref must be a bounded non-empty string")
    try:
        encoded_ref = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ReviewPolicyError("expected base ref must be valid UTF-8") from error
    if len(encoded_ref) > MAX_BASE_REF_BYTES:
        raise ReviewPolicyError("expected base ref must be a bounded non-empty string")
    if value != value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ReviewPolicyError("expected base ref contains unsafe characters")
    return value


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        raise ReviewPolicyError(f"{label} must be a bounded UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReviewPolicyError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewPolicyError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _is_actor(record: dict[str, Any], *, user_id: int, login: str) -> bool:
    user = record.get("user")
    if not isinstance(user, dict):
        return False
    return user.get("id") == user_id and user.get("login") == login


def _is_workflow_request_author(record: dict[str, Any]) -> bool:
    return _is_actor(
        record,
        user_id=GITHUB_ACTIONS_USER_ID,
        login=GITHUB_ACTIONS_LOGIN,
    )


def _pull_request_metadata_digest(title: object, body: object) -> str:
    """Return a bounded digest of review-relevant mutable PR metadata."""
    if not isinstance(title, str) or not isinstance(body, str):
        raise ReviewPolicyError("pull request title and body must be strings")
    normalized_title = title.replace("\r\n", "\n").replace("\r", "\n")
    normalized_body = body.replace("\r\n", "\n").replace("\r", "\n")
    try:
        encoded_title = normalized_title.encode("utf-8")
        encoded_body = normalized_body.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ReviewPolicyError("pull request title and body must be valid UTF-8") from error
    if not encoded_title or len(encoded_title) > MAX_PULL_REQUEST_TITLE_BYTES:
        raise ReviewPolicyError("pull request title exceeds its byte limit")
    if len(encoded_body) > MAX_PULL_REQUEST_BODY_BYTES:
        raise ReviewPolicyError("pull request body exceeds its byte limit")
    framed = (
        len(encoded_title).to_bytes(4, "big")
        + encoded_title
        + len(encoded_body).to_bytes(4, "big")
        + encoded_body
    )
    return hashlib.sha256(framed).hexdigest()


def _load_event_pull_request_metadata(event_path: object) -> tuple[str, str]:
    """Load bounded title/body metadata from GitHub's trusted event file."""
    if not isinstance(event_path, str) or not event_path:
        raise ReviewPolicyError("GitHub event path must be a non-empty string")
    try:
        with open(event_path, "rb") as event_file:
            encoded_event = event_file.read(MAX_EVENT_PAYLOAD_BYTES + 1)
    except (OSError, ValueError) as error:
        raise ReviewPolicyError("GitHub event payload could not be read") from error
    if len(encoded_event) > MAX_EVENT_PAYLOAD_BYTES:
        raise ReviewPolicyError("GitHub event payload exceeds its byte limit")
    try:
        payload = json.loads(encoded_event.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewPolicyError("GitHub event payload is not valid UTF-8 JSON") from error
    event = _record(payload, "GitHub event payload")
    pull_request = _record(event.get("pull_request"), "GitHub event pull request")
    title = pull_request.get("title")
    body = pull_request.get("body")
    if body is None:
        body = ""
    _pull_request_metadata_digest(title, body)
    return cast(str, title), cast(str, body)


def _pull_request_lifecycle_digest(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    action: str,
    baseline_time: datetime,
) -> str:
    """Return a canonical digest of lifecycle events through the trigger."""
    records = api.paginate(f"/repos/{repository}/issues/{pull_request_number}/events")
    lifecycle: list[tuple[int, str, datetime]] = []
    observed_ids: set[int] = set()
    for record in records:
        event = record.get("event")
        if not isinstance(event, str) or len(event) > 64:
            raise ReviewPolicyError("pull request issue event type is invalid")
        if event not in LIFECYCLE_EVENTS:
            continue
        event_id = _positive_int(record.get("id"), "pull request lifecycle event ID")
        if event_id in observed_ids:
            raise ReviewPolicyError("pull request lifecycle event ID is repeated")
        observed_ids.add(event_id)
        created_at = _timestamp(record.get("created_at"), "pull request lifecycle event")
        lifecycle.append((event_id, event, created_at))

    later_events = [event for event in lifecycle if event[2] > baseline_time]
    if later_events:
        raise ReviewPolicyError("pull request lifecycle changed after the triggering event")
    baseline_events = [event for event in lifecycle if event[2] == baseline_time]
    expected_event = LIFECYCLE_ACTION_EVENTS.get(action)
    if expected_event is None:
        if baseline_events:
            raise ReviewPolicyError("pull request lifecycle is ambiguous at the event baseline")
    elif len(baseline_events) != 1 or baseline_events[0][1] != expected_event:
        raise ReviewPolicyError("pull request lifecycle does not match the triggering event")

    canonical = [
        (event_id, event, created_at.isoformat())
        for event_id, event, created_at in sorted(
            lifecycle,
            key=lambda item: (item[2], item[0], item[1]),
        )
    ]
    encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _load_pull_request(api: RestApi, repository: str, number: int) -> PullRequest:
    payload = _record(api.get(f"/repos/{repository}/pulls/{number}"), "pull request")
    payload_number = _positive_int(payload.get("number"), "pull request number")
    if payload_number != number:
        raise ReviewPolicyError("GitHub returned a different pull request")
    head = _record(payload.get("head"), "pull request head")
    sha = head.get("sha")
    base = _record(payload.get("base"), "pull request base")
    base_ref = _base_ref(base.get("ref"))
    base_sha = base.get("sha")
    author = _record(payload.get("user"), "pull request author")
    author_id = _positive_int(author.get("id"), "pull request author ID")
    draft = payload.get("draft")
    state = payload.get("state")
    updated_at = _timestamp(payload.get("updated_at"), "pull request update")
    body = payload.get("body")
    if body is None:
        body = ""
    metadata_digest = _pull_request_metadata_digest(
        payload.get("title"),
        body,
    )
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ReviewPolicyError("pull request head is not a full lowercase commit SHA")
    if not isinstance(base_sha, str) or not SHA_RE.fullmatch(base_sha):
        raise ReviewPolicyError("pull request base is not a full lowercase commit SHA")
    if not isinstance(draft, bool):
        raise ReviewPolicyError("pull request draft state is invalid")
    if not isinstance(state, str):
        raise ReviewPolicyError("pull request state is invalid")
    return PullRequest(
        number=payload_number,
        head_sha=sha,
        base_ref=base_ref,
        base_sha=base_sha,
        author_id=author_id,
        draft=draft,
        state=state,
        updated_at=updated_at,
        metadata_digest=metadata_digest,
    )


def _eligible_pull_request(
    api: RestApi,
    repository: str,
    number: int,
    expected_head: str,
    expected_base_ref: str,
    expected_base_sha: str,
) -> PullRequest:
    pull_request = _load_pull_request(api, repository, number)
    if pull_request.state != "open":
        raise ReviewPolicyError("pull request is not open")
    if pull_request.draft:
        raise ReviewPolicyError("draft pull requests cannot satisfy review policy")
    if pull_request.head_sha != expected_head:
        raise ReviewPolicyError("live pull request head does not match the expected head")
    if pull_request.base_ref != expected_base_ref:
        raise ReviewPolicyError("live pull request base ref does not match the expected base ref")
    if pull_request.base_sha != expected_base_sha:
        raise ReviewPolicyError("live pull request base SHA does not match the expected base SHA")
    return pull_request


def _confirm_live_candidate(
    api: RestApi,
    repository: str,
    number: int,
    expected_head: str,
    expected_base_ref: str,
    expected_base_sha: str,
) -> PullRequest:
    """Close head/base races between observing a signal and returning success."""
    return _eligible_pull_request(
        api,
        repository,
        number,
        expected_head,
        expected_base_ref,
        expected_base_sha,
    )


def _confirm_event_bound_candidate(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_head: str,
    expected_base_ref: str,
    expected_base_sha: str,
    expected_metadata_digest: str,
    expected_lifecycle_digest: str,
    action: str,
    baseline_time: datetime,
    phase: str,
) -> PullRequest:
    """Confirm one stable live/lifecycle snapshot before returning success."""
    before = _confirm_live_candidate(
        api,
        repository,
        pull_request_number,
        expected_head,
        expected_base_ref,
        expected_base_sha,
    )
    first_lifecycle_digest = _pull_request_lifecycle_digest(
        api,
        repository=repository,
        pull_request_number=pull_request_number,
        action=action,
        baseline_time=baseline_time,
    )
    after = _confirm_live_candidate(
        api,
        repository,
        pull_request_number,
        expected_head,
        expected_base_ref,
        expected_base_sha,
    )
    second_lifecycle_digest = _pull_request_lifecycle_digest(
        api,
        repository=repository,
        pull_request_number=pull_request_number,
        action=action,
        baseline_time=baseline_time,
    )
    if before.updated_at != after.updated_at:
        raise ReviewPolicyError(f"pull request changed {phase} final confirmation")
    if (
        before.metadata_digest != expected_metadata_digest
        or after.metadata_digest != expected_metadata_digest
    ):
        raise ReviewPolicyError(f"pull request title or body changed {phase}")
    if (
        first_lifecycle_digest != expected_lifecycle_digest
        or second_lifecycle_digest != expected_lifecycle_digest
    ):
        raise ReviewPolicyError(f"pull request lifecycle changed {phase}")
    return after


def _workflow_run_pull_request(
    payload: dict[str, Any],
    *,
    pull_request_number: int,
    expected_head: str,
    expected_base_ref: str | None = None,
    expected_base_sha: str | None = None,
) -> bool:
    pull_requests = payload.get("pull_requests")
    if not isinstance(pull_requests, list) or len(pull_requests) > MAX_REQUEST_COMMENT_CANDIDATES:
        raise ReviewPolicyError("Codex workflow run pull request associations are invalid")
    matches: list[dict[str, Any]] = []
    for value in pull_requests:
        pull_request = _record(value, "Codex workflow run pull request")
        number = _positive_int(pull_request.get("number"), "workflow run pull request number")
        head = _record(pull_request.get("head"), "Codex workflow run pull request head")
        head_sha = _commit_sha(head.get("sha"), "workflow run pull request head")
        if number != pull_request_number:
            continue
        if head_sha != expected_head:
            raise ReviewPolicyError(
                "Codex workflow run identifies the pull request with a different head"
            )
        matches.append(pull_request)
    if len(matches) > 1:
        raise ReviewPolicyError("Codex workflow run repeats the pull request association")
    if not matches:
        return False
    if expected_base_ref is None and expected_base_sha is None:
        return True
    if expected_base_ref is None or expected_base_sha is None:
        raise ReviewPolicyError("Codex workflow run base expectation is incomplete")
    pull_request = matches[0]
    base = _record(pull_request.get("base"), "Codex workflow run pull request base")
    if _base_ref(base.get("ref")) != expected_base_ref:
        raise ReviewPolicyError("Codex workflow run identifies a different pull request base ref")
    if _commit_sha(base.get("sha"), "workflow run pull request base") != expected_base_sha:
        raise ReviewPolicyError("Codex workflow run identifies a different pull request base SHA")
    return True


def _workflow_run_identity(
    payload: dict[str, Any],
    *,
    expected_head: str,
) -> WorkflowRun:
    run_id = _positive_int(payload.get("id"), "Codex workflow run ID")
    run_number = _positive_int(payload.get("run_number"), "Codex workflow run number")
    run_attempt = _positive_int(payload.get("run_attempt"), "Codex workflow run attempt")
    if run_attempt > MAX_RUN_ATTEMPT:
        raise ReviewPolicyError(f"workflow run attempt must be between 1 and {MAX_RUN_ATTEMPT}")
    if payload.get("event") != "pull_request_target":
        raise ReviewPolicyError("Codex workflow run has an unexpected event")
    if payload.get("path") != CODEX_WORKFLOW_PATH:
        raise ReviewPolicyError("Codex workflow run has an unexpected workflow path")
    if _commit_sha(payload.get("head_sha"), "Codex workflow run head") != expected_head:
        raise ReviewPolicyError("Codex workflow run identifies a different head")
    created_at = _timestamp(payload.get("created_at"), "Codex workflow run creation")
    run_started_at = _timestamp(payload.get("run_started_at"), "Codex workflow run start")
    if run_started_at < created_at:
        raise ReviewPolicyError("Codex workflow run started before it was created")
    return WorkflowRun(
        run_id=run_id,
        run_number=run_number,
        run_attempt=run_attempt,
        created_at=created_at,
        run_started_at=run_started_at,
    )


def _load_current_codex_workflow_run(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_head: str,
    expected_base_ref: str,
    expected_base_sha: str,
    workflow_run_id: int,
    run_attempt: int,
) -> WorkflowRun:
    payload = _record(
        api.get(f"/repos/{repository}/actions/runs/{workflow_run_id}"),
        "current Codex workflow run",
    )
    workflow_run = _workflow_run_identity(
        payload,
        expected_head=expected_head,
    )
    if workflow_run.run_id != workflow_run_id:
        raise ReviewPolicyError("GitHub returned a different Codex workflow run")
    if workflow_run.run_attempt != run_attempt:
        raise ReviewPolicyError("Codex workflow run attempt does not match the current attempt")
    if not _workflow_run_pull_request(
        payload,
        pull_request_number=pull_request_number,
        expected_head=expected_head,
        expected_base_ref=expected_base_ref,
        expected_base_sha=expected_base_sha,
    ):
        raise ReviewPolicyError("Codex workflow run does not identify this pull request")
    return workflow_run


def _confirm_codex_workflow_run_is_current(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_head: str,
    workflow_run: WorkflowRun,
) -> bool:
    created_bound = workflow_run.created_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    query = urllib.parse.urlencode(
        {
            "event": "pull_request_target",
            "head_sha": expected_head,
            "created": f">={created_bound}",
            "per_page": str(MAX_WORKFLOW_RUN_RECORDS),
        }
    )
    payload = _record(
        api.get(f"/repos/{repository}/actions/workflows/{CODEX_WORKFLOW_FILE}/runs?{query}"),
        "Codex workflow runs",
    )
    total_count = payload.get("total_count")
    records = payload.get("workflow_runs")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
        or total_count > MAX_WORKFLOW_RUN_RECORDS
        or not isinstance(records, list)
        or len(records) != total_count
    ):
        raise ReviewPolicyError("Codex workflow run history is incomplete or unbounded")

    current_matches = 0
    for value in records:
        record = _record(value, "Codex workflow run history record")
        candidate = _workflow_run_identity(
            record,
            expected_head=expected_head,
        )
        if candidate.created_at < workflow_run.created_at:
            raise ReviewPolicyError("Codex workflow run history ignored its creation bound")
        if not _workflow_run_pull_request(
            record,
            pull_request_number=pull_request_number,
            expected_head=expected_head,
        ):
            continue
        if candidate.run_id == workflow_run.run_id:
            current_matches += 1
            if (
                candidate.run_number != workflow_run.run_number
                or candidate.run_attempt != workflow_run.run_attempt
                or candidate.created_at != workflow_run.created_at
                or candidate.run_started_at != workflow_run.run_started_at
            ):
                raise ReviewPolicyError("current Codex workflow run identity changed")
        elif candidate.run_number >= workflow_run.run_number:
            raise ReviewPolicyError(
                "Codex workflow run was superseded by a newer pull request event"
            )
    if current_matches > 1:
        raise ReviewPolicyError("current Codex workflow run is duplicated in bounded history")
    return current_matches == 1


def _reviews(api: RestApi, repository: str, number: int) -> list[dict[str, Any]]:
    return api.paginate(f"/repos/{repository}/pulls/{number}/reviews")


def _meaningful_maintainer_review(
    reviews: list[dict[str, Any]], expected_head: str
) -> dict[str, Any] | None:
    candidates: list[tuple[datetime, int, dict[str, Any]]] = []
    for review in reviews:
        if (
            not _is_actor(review, user_id=MAINTAINER_USER_ID, login=MAINTAINER_LOGIN)
            or review.get("commit_id") != expected_head
        ):
            continue
        state = review.get("state")
        if state not in MEANINGFUL_MAINTAINER_STATES:
            continue
        review_id = _positive_int(review.get("id"), "maintainer review ID")
        submitted_at = _timestamp(review.get("submitted_at"), "maintainer review timestamp")
        candidates.append((submitted_at, review_id, review))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def check_maintainer_policy(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_head: str,
    expected_base_ref: str,
    expected_base_sha: str,
) -> str:
    """Require the owner on external PRs without requiring self-approval."""
    repository = _repository(repository)
    pull_request_number = _positive_int(pull_request_number, "pull request number")
    expected_head = _commit_sha(expected_head, "expected head")
    expected_base_ref = _base_ref(expected_base_ref)
    expected_base_sha = _commit_sha(expected_base_sha, "expected base SHA")
    pull_request = _eligible_pull_request(
        api,
        repository,
        pull_request_number,
        expected_head,
        expected_base_ref,
        expected_base_sha,
    )
    if pull_request.author_id == MAINTAINER_USER_ID:
        _confirm_live_candidate(
            api,
            repository,
            pull_request_number,
            expected_head,
            expected_base_ref,
            expected_base_sha,
        )
        return "owner-authored pull request; self-approval is not required"

    review = _meaningful_maintainer_review(
        _reviews(api, repository, pull_request_number), expected_head
    )
    if review is None:
        raise ReviewPolicyError(
            "external pull request needs an exact-head approval from the maintainer"
        )
    state = review.get("state")
    if state != "APPROVED":
        raise ReviewPolicyError(f"latest meaningful exact-head maintainer review is {state}")
    _confirm_live_candidate(
        api,
        repository,
        pull_request_number,
        expected_head,
        expected_base_ref,
        expected_base_sha,
    )
    return "external pull request has an exact-head maintainer approval"


def codex_request_body(
    expected_head: str,
    *,
    workflow_run_id: int,
    run_attempt: int,
    metadata_digest: str,
    lifecycle_digest: str,
) -> str:
    """Return the exact body for a head- and workflow-attempt-bound request."""
    expected_head = _commit_sha(expected_head, "expected head")
    workflow_run_id = _positive_int(workflow_run_id, "Codex workflow run ID")
    if (
        isinstance(run_attempt, bool)
        or not isinstance(run_attempt, int)
        or not 1 <= run_attempt <= MAX_RUN_ATTEMPT
    ):
        raise ReviewPolicyError(f"workflow run attempt must be between 1 and {MAX_RUN_ATTEMPT}")
    if not METADATA_DIGEST_RE.fullmatch(metadata_digest):
        raise ReviewPolicyError("pull request metadata digest is invalid")
    if not METADATA_DIGEST_RE.fullmatch(lifecycle_digest):
        raise ReviewPolicyError("pull request lifecycle digest is invalid")
    return (
        f"@codex review\n\n<!-- django-ray:codex-review-head={expected_head};"
        f"run={workflow_run_id};attempt={run_attempt};metadata={metadata_digest};"
        f"lifecycle={lifecycle_digest} -->"
    )


def _connector_eyes_started_at(records: list[dict[str, Any]]) -> datetime | None:
    """Return the newest current Codex in-progress reaction timestamp."""
    candidates: list[datetime] = []
    for reaction in records:
        if (
            not _is_actor(
                reaction,
                user_id=CODEX_CONNECTOR_USER_ID,
                login=CODEX_CONNECTOR_LOGIN,
            )
            or reaction.get("content") != "eyes"
        ):
            continue
        _positive_int(reaction.get("id"), "Codex reaction ID")
        candidates.append(_timestamp(reaction.get("created_at"), "Codex reaction timestamp"))
    return max(candidates) if candidates else None


def _load_connector_eyes_started_at(api: RestApi, path: str) -> datetime | None:
    """Read one bounded eyes-only page and reject an ambiguous full page."""
    separator = "&" if "?" in path else "?"
    payload = api.get(f"{path}{separator}content=eyes&per_page={REACTION_PAGE_SIZE}&page=1")
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ReviewPolicyError("Codex eyes query returned an invalid page")
    reactions = cast(list[dict[str, Any]], payload)
    if len(reactions) > REACTION_PAGE_SIZE:
        raise ReviewPolicyError("Codex eyes query exceeded its page limit")
    started_at = _connector_eyes_started_at(reactions)
    if len(reactions) == REACTION_PAGE_SIZE and started_at is None:
        raise ReviewPolicyError("Codex eyes query is ambiguous at its page limit")
    return started_at


def _root_review_started_at(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
) -> datetime | None:
    return _load_connector_eyes_started_at(
        api,
        f"/repos/{repository}/issues/{pull_request_number}/reactions",
    )


def _request_review_started_at(
    api: RestApi,
    *,
    repository: str,
    request_comment_id: int,
) -> datetime | None:
    return _load_connector_eyes_started_at(
        api,
        f"/repos/{repository}/issues/comments/{request_comment_id}/reactions",
    )


def _request_comment_update(
    comment: dict[str, Any],
    *,
    repository: str,
    pull_request_number: int,
    expected_body: str,
    baseline_time: datetime,
    require_match: bool,
) -> datetime | None:
    body = comment.get("body")
    if not isinstance(body, str) or len(body.encode("utf-8")) > 1_024:
        if require_match:
            raise ReviewPolicyError("Codex request comment body is invalid")
        return None
    normalized_body = body.replace("\r\n", "\n").strip()
    if normalized_body != expected_body:
        if require_match:
            raise ReviewPolicyError("Codex request comment is not bound to the expected head")
        return None
    if not _is_workflow_request_author(comment):
        if require_match:
            raise ReviewPolicyError("Codex request comment was not created by the trusted workflow")
        return None
    issue_url = comment.get("issue_url")
    if not isinstance(issue_url, str) or len(issue_url) > 2_048:
        if require_match:
            raise ReviewPolicyError("Codex request comment issue URL is invalid")
        return None
    expected_issue_path = f"/repos/{repository}/issues/{pull_request_number}".lower()
    if urllib.parse.urlsplit(issue_url).path.lower() != expected_issue_path:
        if require_match:
            raise ReviewPolicyError("Codex request comment does not belong to this pull request")
        return None
    try:
        created_at = _timestamp(comment.get("created_at"), "Codex request comment creation")
        updated_at = _timestamp(comment.get("updated_at"), "Codex request comment update")
    except ReviewPolicyError:
        if require_match:
            raise
        return None
    if created_at < baseline_time or updated_at < created_at:
        if require_match:
            raise ReviewPolicyError("Codex request comment predates the frozen event baseline")
        return None
    return updated_at


def ensure_codex_request_comment(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_head: str,
    expected_base_ref: str,
    expected_base_sha: str,
    action: str,
    baseline_time: str | datetime,
    workflow_run_id: int,
    run_attempt: int,
    expected_title: str,
    expected_body: str,
) -> int:
    """Create or reuse the trusted exact-head request for one PR event."""
    repository = _repository(repository)
    pull_request_number = _positive_int(pull_request_number, "pull request number")
    expected_head = _commit_sha(expected_head, "expected head")
    expected_base_ref = _base_ref(expected_base_ref)
    expected_base_sha = _commit_sha(expected_base_sha, "expected base SHA")
    workflow_run_id = _positive_int(workflow_run_id, "Codex workflow run ID")
    if action not in ELIGIBLE_CODEX_ACTIONS:
        raise ReviewPolicyError("unsupported Codex request action")
    parsed_baseline = (
        baseline_time.astimezone(UTC)
        if isinstance(baseline_time, datetime)
        and baseline_time.tzinfo is not None
        and baseline_time.utcoffset() is not None
        else _timestamp(baseline_time, "event baseline")
    )
    pull_request = _eligible_pull_request(
        api,
        repository,
        pull_request_number,
        expected_head,
        expected_base_ref,
        expected_base_sha,
    )
    expected_metadata_digest = _pull_request_metadata_digest(expected_title, expected_body)
    if pull_request.metadata_digest != expected_metadata_digest:
        raise ReviewPolicyError("live pull request title or body does not match the event")
    expected_lifecycle_digest = _pull_request_lifecycle_digest(
        api,
        repository=repository,
        pull_request_number=pull_request_number,
        action=action,
        baseline_time=parsed_baseline,
    )
    workflow_run = _load_current_codex_workflow_run(
        api,
        repository=repository,
        pull_request_number=pull_request_number,
        expected_head=expected_head,
        expected_base_ref=expected_base_ref,
        expected_base_sha=expected_base_sha,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
    )
    request_not_before = max(parsed_baseline, workflow_run.run_started_at)
    body = codex_request_body(
        expected_head,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        metadata_digest=expected_metadata_digest,
        lifecycle_digest=expected_lifecycle_digest,
    )
    candidates: list[tuple[datetime, int]] = []
    comments = api.paginate(f"/repos/{repository}/issues/{pull_request_number}/comments")
    for comment in comments:
        if comment.get("body") != body or not _is_actor(
            comment,
            user_id=GITHUB_ACTIONS_USER_ID,
            login=GITHUB_ACTIONS_LOGIN,
        ):
            continue
        updated_at = _request_comment_update(
            comment,
            repository=repository,
            pull_request_number=pull_request_number,
            expected_body=body,
            baseline_time=request_not_before,
            require_match=False,
        )
        if updated_at is not None:
            candidates.append(
                (
                    updated_at,
                    _positive_int(comment.get("id"), "Codex request comment ID"),
                )
            )
    if candidates:
        comment_id = max(candidates)[1]
    else:
        comment = _record(
            api.post(
                f"/repos/{repository}/issues/{pull_request_number}/comments",
                {"body": body},
            ),
            "created Codex request comment",
        )
        if not _is_actor(
            comment,
            user_id=GITHUB_ACTIONS_USER_ID,
            login=GITHUB_ACTIONS_LOGIN,
        ):
            raise ReviewPolicyError("created Codex request comment has an unexpected author")
        _request_comment_update(
            comment,
            repository=repository,
            pull_request_number=pull_request_number,
            expected_body=body,
            baseline_time=request_not_before,
            require_match=True,
        )
        comment_id = _positive_int(comment.get("id"), "Codex request comment ID")
    _confirm_event_bound_candidate(
        api,
        repository=repository,
        pull_request_number=pull_request_number,
        expected_head=expected_head,
        expected_base_ref=expected_base_ref,
        expected_base_sha=expected_base_sha,
        expected_metadata_digest=expected_metadata_digest,
        expected_lifecycle_digest=expected_lifecycle_digest,
        action=action,
        baseline_time=parsed_baseline,
        phase="while requesting review",
    )
    return comment_id


def _request_comment_state_by_id(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_body: str,
    baseline_time: datetime,
    request_comment_id: int,
) -> tuple[datetime, datetime | None]:
    comment = _record(
        api.get(f"/repos/{repository}/issues/comments/{request_comment_id}"),
        "Codex request comment",
    )
    if _positive_int(comment.get("id"), "Codex request comment ID") != request_comment_id:
        raise ReviewPolicyError("GitHub returned a different Codex request comment")
    updated_at = _request_comment_update(
        comment,
        repository=repository,
        pull_request_number=pull_request_number,
        expected_body=expected_body,
        baseline_time=baseline_time,
        require_match=True,
    )
    if updated_at is None:
        raise ReviewPolicyError("Codex request comment is not eligible")
    review_started_at = _request_review_started_at(
        api,
        repository=repository,
        request_comment_id=request_comment_id,
    )
    return updated_at, review_started_at


def _confirm_codex_settlement(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_body: str,
    baseline_time: datetime,
    request_comment_id: int,
    request_not_before: datetime,
) -> bool:
    """Re-read the connector's mutable in-progress state before success."""
    _updated_at, request_review_started_at = _request_comment_state_by_id(
        api,
        repository=repository,
        pull_request_number=pull_request_number,
        expected_body=expected_body,
        baseline_time=baseline_time,
        request_comment_id=request_comment_id,
    )
    root_review_started_at = _root_review_started_at(
        api,
        repository=repository,
        pull_request_number=pull_request_number,
    )
    if _updated_at != request_not_before:
        raise ReviewPolicyError("Codex request comment changed during review")
    return request_review_started_at is None and root_review_started_at is None


def _bounded_poll_values(timeout: float, interval: float) -> tuple[float, float]:
    if not 0.0 <= timeout <= MAX_POLL_TIMEOUT_SECONDS:
        raise ReviewPolicyError(
            f"poll timeout must be between 0 and {MAX_POLL_TIMEOUT_SECONDS:g} seconds"
        )
    if not 1.0 <= interval <= MAX_POLL_INTERVAL_SECONDS:
        raise ReviewPolicyError(
            f"poll interval must be between 1 and {MAX_POLL_INTERVAL_SECONDS:g} seconds"
        )
    return timeout, interval


def check_codex_policy(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_head: str,
    expected_base_ref: str,
    expected_base_sha: str,
    action: str,
    baseline_time: str | datetime,
    workflow_run_id: int,
    run_attempt: int,
    expected_title: str,
    expected_body: str,
    request_comment_id: int | None = None,
    poll_timeout: float = 600.0,
    poll_interval: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Poll one workflow-authored request until Codex settles its review."""
    repository = _repository(repository)
    pull_request_number = _positive_int(pull_request_number, "pull request number")
    expected_head = _commit_sha(expected_head, "expected head")
    expected_base_ref = _base_ref(expected_base_ref)
    expected_base_sha = _commit_sha(expected_base_sha, "expected base SHA")
    workflow_run_id = _positive_int(workflow_run_id, "Codex workflow run ID")
    if (
        isinstance(run_attempt, bool)
        or not isinstance(run_attempt, int)
        or not 1 <= run_attempt <= MAX_RUN_ATTEMPT
    ):
        raise ReviewPolicyError(f"workflow run attempt must be between 1 and {MAX_RUN_ATTEMPT}")
    if action not in CODEX_ACTIONS:
        raise ReviewPolicyError("unsupported pull request action")
    parsed_baseline = (
        baseline_time.astimezone(UTC)
        if isinstance(baseline_time, datetime)
        and baseline_time.tzinfo is not None
        and baseline_time.utcoffset() is not None
        else _timestamp(baseline_time, "event baseline")
    )
    poll_timeout, poll_interval = _bounded_poll_values(poll_timeout, poll_interval)

    pull_request = _eligible_pull_request(
        api,
        repository,
        pull_request_number,
        expected_head,
        expected_base_ref,
        expected_base_sha,
    )
    expected_metadata_digest = _pull_request_metadata_digest(expected_title, expected_body)
    if pull_request.metadata_digest != expected_metadata_digest:
        raise ReviewPolicyError("live pull request title or body does not match the event")
    if action in TERMINAL_CODEX_ACTIONS:
        raise ReviewPolicyError(f"{action.replace('_', '-')} pull requests cannot satisfy policy")
    expected_lifecycle_digest = _pull_request_lifecycle_digest(
        api,
        repository=repository,
        pull_request_number=pull_request_number,
        action=action,
        baseline_time=parsed_baseline,
    )
    if request_comment_id is None:
        raise ReviewPolicyError("Codex mode requires a workflow-authored request comment")
    request_comment_id = _positive_int(request_comment_id, "Codex request comment ID")
    workflow_run = _load_current_codex_workflow_run(
        api,
        repository=repository,
        pull_request_number=pull_request_number,
        expected_head=expected_head,
        expected_base_ref=expected_base_ref,
        expected_base_sha=expected_base_sha,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
    )
    request_body = codex_request_body(
        expected_head,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        metadata_digest=expected_metadata_digest,
        lifecycle_digest=expected_lifecycle_digest,
    )
    request_not_before, current_request_review_started_at = _request_comment_state_by_id(
        api,
        repository=repository,
        pull_request_number=pull_request_number,
        expected_body=request_body,
        baseline_time=parsed_baseline,
        request_comment_id=request_comment_id,
    )
    observed_request_review_started_at = (
        current_request_review_started_at
        if current_request_review_started_at is not None
        and current_request_review_started_at >= request_not_before
        else None
    )
    settled_poll_observed = False
    deadline = monotonic() + poll_timeout
    while True:
        current_request_review_started_at = _request_review_started_at(
            api,
            repository=repository,
            request_comment_id=request_comment_id,
        )
        if (
            current_request_review_started_at is not None
            and current_request_review_started_at >= request_not_before
        ):
            observed_request_review_started_at = max(
                observed_request_review_started_at or current_request_review_started_at,
                current_request_review_started_at,
            )
        root_review_started_at = _root_review_started_at(
            api,
            repository=repository,
            pull_request_number=pull_request_number,
        )
        review_in_progress = (
            current_request_review_started_at is not None or root_review_started_at is not None
        )
        if review_in_progress:
            settled_poll_observed = False
        elif observed_request_review_started_at is not None:
            if settled_poll_observed:
                if _confirm_codex_workflow_run_is_current(
                    api,
                    repository=repository,
                    pull_request_number=pull_request_number,
                    expected_head=expected_head,
                    workflow_run=workflow_run,
                ) and _confirm_codex_settlement(
                    api,
                    repository=repository,
                    pull_request_number=pull_request_number,
                    expected_body=request_body,
                    baseline_time=parsed_baseline,
                    request_comment_id=request_comment_id,
                    request_not_before=request_not_before,
                ):
                    _confirm_event_bound_candidate(
                        api,
                        repository=repository,
                        pull_request_number=pull_request_number,
                        expected_head=expected_head,
                        expected_base_ref=expected_base_ref,
                        expected_base_sha=expected_base_sha,
                        expected_metadata_digest=expected_metadata_digest,
                        expected_lifecycle_digest=expected_lifecycle_digest,
                        action=action,
                        baseline_time=parsed_baseline,
                        phase="during Codex review",
                    )
                    return "Codex connector review request settled"
                settled_poll_observed = False
            else:
                # Require a second eye-free observation so a transient API
                # update cannot green the gate between connector states.
                settled_poll_observed = True

        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(poll_interval, remaining))
    raise ReviewPolicyError("timed out waiting for the exact-head Codex review request to settle")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("maintainer", "codex-request", "codex"),
        required=True,
    )
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-base-ref", required=True)
    parser.add_argument("--expected-base-sha", required=True)
    parser.add_argument("--event-path")
    parser.add_argument("--action", choices=CODEX_ACTIONS)
    parser.add_argument("--baseline-time")
    parser.add_argument("--workflow-run-id", type=int)
    parser.add_argument("--run-attempt", type=int)
    parser.add_argument("--request-comment-id", type=int)
    parser.add_argument("--poll-timeout", type=float, default=600.0)
    parser.add_argument("--poll-interval", type=float, default=60.0)
    parser.add_argument("--request-timeout", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one policy and return a workflow-friendly status code."""
    args = _parser().parse_args(argv)
    try:
        if args.repository is None:
            raise ReviewPolicyError("--repository or GITHUB_REPOSITORY is required")
        api = GitHubRestApi(
            os.environ.get("GITHUB_TOKEN", ""),
            base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            request_timeout=args.request_timeout,
        )
        common = {
            "repository": args.repository,
            "pull_request_number": args.pull_request,
            "expected_head": args.expected_head,
            "expected_base_ref": args.expected_base_ref,
            "expected_base_sha": args.expected_base_sha,
        }
        if args.mode == "maintainer":
            if (
                args.action is not None
                or args.baseline_time is not None
                or args.workflow_run_id is not None
                or args.run_attempt is not None
                or args.request_comment_id is not None
                or args.event_path is not None
            ):
                raise ReviewPolicyError("Codex-only arguments cannot be used in maintainer mode")
            result = check_maintainer_policy(api, **common)
        elif args.mode == "codex-request":
            if (
                args.action is None
                or args.baseline_time is None
                or args.workflow_run_id is None
                or args.run_attempt is None
                or args.event_path is None
            ):
                raise ReviewPolicyError(
                    "Codex request mode requires --action, --baseline-time, "
                    "--workflow-run-id, --run-attempt, and --event-path"
                )
            if args.request_comment_id is not None:
                raise ReviewPolicyError("--request-comment-id cannot be used in Codex request mode")
            expected_title, expected_body = _load_event_pull_request_metadata(args.event_path)
            result = ensure_codex_request_comment(
                api,
                **common,
                action=args.action,
                baseline_time=args.baseline_time,
                workflow_run_id=args.workflow_run_id,
                run_attempt=args.run_attempt,
                expected_title=expected_title,
                expected_body=expected_body,
            )
        else:
            if (
                args.action is None
                or args.baseline_time is None
                or args.workflow_run_id is None
                or args.run_attempt is None
                or args.event_path is None
            ):
                raise ReviewPolicyError(
                    "Codex mode requires --action, --baseline-time, "
                    "--workflow-run-id, --run-attempt, and --event-path"
                )
            expected_title, expected_body = _load_event_pull_request_metadata(args.event_path)
            result = check_codex_policy(
                api,
                **common,
                action=args.action,
                baseline_time=args.baseline_time,
                workflow_run_id=args.workflow_run_id,
                run_attempt=args.run_attempt,
                expected_title=expected_title,
                expected_body=expected_body,
                request_comment_id=args.request_comment_id,
                poll_timeout=args.poll_timeout,
                poll_interval=args.poll_interval,
            )
    except ReviewPolicyError as error:
        print(f"review policy failed: {error}", file=sys.stderr)
        return 1
    if args.mode == "codex-request":
        print(result)
        return 0
    print(f"review policy passed: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
