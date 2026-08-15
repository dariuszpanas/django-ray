"""Fail-closed GitHub pull-request review policy checks.

This script is intended for a trusted ``pull_request_target`` workflow.  It
uses only GitHub REST responses and never checks out or executes pull-request
code.
"""

from __future__ import annotations

import argparse
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
MAINTAINER_LOGIN = "dariuszpanas"
CODEX_CONNECTOR_LOGIN = "chatgpt-codex-connector[bot]"

API_VERSION = "2022-11-28"
MAX_API_PAGES = 10
MAX_API_RECORDS = 1_000
MAX_API_REQUESTS = 100
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_GITHUB_ID = 9_223_372_036_854_775_807
MAX_BASE_REF_BYTES = 255
MAX_RUN_ATTEMPT = 10_000
MAX_POLL_TIMEOUT_SECONDS = 900.0
MAX_POLL_INTERVAL_SECONDS = 60.0
MAX_REQUEST_TIMEOUT_SECONDS = 30.0
MAX_REQUEST_COMMENT_CANDIDATES = 20
MAX_WORKFLOW_RUN_RECORDS = 100

CODEX_WORKFLOW_FILE = "codex-review.yml"
CODEX_WORKFLOW_PATH = f".github/workflows/{CODEX_WORKFLOW_FILE}"

REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
ELIGIBLE_CODEX_ACTIONS = frozenset(
    {"edited", "opened", "ready_for_review", "synchronize", "reopened"}
)
CODEX_ACTIONS = (*sorted(ELIGIBLE_CODEX_ACTIONS), "converted_to_draft")
COMMENT_SIGNAL_ACTIONS = frozenset({"edited", "synchronize", "reopened"})
ROOT_REACTION_ACTIONS = frozenset({"opened", "ready_for_review"})
MEANINGFUL_MAINTAINER_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})
ACCEPTED_CODEX_REVIEW_STATES = frozenset({"APPROVED", "COMMENTED"})


class ReviewPolicyError(RuntimeError):
    """Raised when evidence is missing, stale, malformed, or unsafe."""


class RestApi(Protocol):
    """The small, read-only GitHub REST surface used by the policies."""

    def get(self, path: str) -> object: ...

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

    def get(self, path: str) -> object:
        """Read and decode one bounded GitHub REST response."""
        if not path.startswith("/") or "\\" in path or "\n" in path or "\r" in path:
            raise ReviewPolicyError("GitHub API path is invalid")
        self._consume_request_budget()
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            method="GET",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "django-ray-review-policy",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - URL is validated and path is internal.
                request, timeout=self._request_timeout
            ) as response:
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise ReviewPolicyError(f"GitHub API GET failed with HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            detail = " ".join(str(getattr(error, "reason", error)).split())[:300]
            raise ReviewPolicyError(f"GitHub API GET failed: {detail}") from error
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise ReviewPolicyError("GitHub API response exceeded the byte limit")
        try:
            return json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReviewPolicyError("GitHub API returned invalid JSON") from error

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


@dataclass(frozen=True)
class WorkflowRun:
    """Trusted identity of the executing Codex review workflow run."""

    run_id: int
    run_number: int
    run_attempt: int
    created_at: datetime


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
    return WorkflowRun(
        run_id=run_id,
        run_number=run_number,
        run_attempt=run_attempt,
        created_at=_timestamp(payload.get("created_at"), "Codex workflow run creation"),
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


def codex_request_body(expected_head: str) -> str:
    """Return the exact, reviewable body for a SHA-bound Codex request."""
    expected_head = _commit_sha(expected_head, "expected head")
    return f"@codex review\n\n<!-- django-ray:codex-review-head={expected_head} -->"


def _codex_review_signal(
    reviews: list[dict[str, Any]],
    *,
    expected_head: str,
    not_before: datetime,
) -> datetime | None:
    candidates: list[tuple[datetime, int]] = []
    for review in reviews:
        if not _is_actor(
            review,
            user_id=CODEX_CONNECTOR_USER_ID,
            login=CODEX_CONNECTOR_LOGIN,
        ):
            continue
        if (
            review.get("commit_id") == expected_head
            and review.get("state") in ACCEPTED_CODEX_REVIEW_STATES
        ):
            review_id = _positive_int(review.get("id"), "Codex review ID")
            submitted_at = _timestamp(review.get("submitted_at"), "Codex review timestamp")
            if submitted_at > not_before:
                candidates.append((submitted_at, review_id))
    return max(candidates)[0] if candidates else None


def _clean_codex_reaction(
    records: list[dict[str, Any]], *, not_before: datetime
) -> datetime | None:
    candidates: list[tuple[datetime, int]] = []
    for reaction in records:
        if (
            not _is_actor(
                reaction,
                user_id=CODEX_CONNECTOR_USER_ID,
                login=CODEX_CONNECTOR_LOGIN,
            )
            or reaction.get("content") != "+1"
        ):
            continue
        reaction_id = _positive_int(reaction.get("id"), "Codex reaction ID")
        created_at = _timestamp(reaction.get("created_at"), "Codex reaction timestamp")
        if created_at > not_before:
            candidates.append((created_at, reaction_id))
    return max(candidates)[0] if candidates else None


def _root_reaction_signal(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    baseline_time: datetime,
) -> datetime | None:
    reactions = api.paginate(f"/repos/{repository}/issues/{pull_request_number}/reactions")
    return _clean_codex_reaction(reactions, not_before=baseline_time)


def _request_comment_update(
    comment: dict[str, Any],
    *,
    repository: str,
    pull_request_number: int,
    expected_head: str,
    baseline_time: datetime,
    require_match: bool,
) -> datetime | None:
    body = comment.get("body")
    if not isinstance(body, str) or len(body.encode("utf-8")) > 1_024:
        if require_match:
            raise ReviewPolicyError("Codex request comment body is invalid")
        return None
    normalized_body = body.replace("\r\n", "\n").strip()
    if normalized_body != codex_request_body(expected_head):
        if require_match:
            raise ReviewPolicyError("Codex request comment is not bound to the expected head")
        return None
    if not _is_actor(comment, user_id=MAINTAINER_USER_ID, login=MAINTAINER_LOGIN):
        if require_match:
            raise ReviewPolicyError("Codex request comment was not authored by the maintainer")
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
    if created_at <= baseline_time or updated_at < created_at:
        if require_match:
            raise ReviewPolicyError("Codex request comment predates the frozen event baseline")
        return None
    return updated_at


def _request_comment_has_reaction(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_head: str,
    baseline_time: datetime,
    comment: dict[str, Any],
    require_match: bool,
) -> datetime | None:
    updated_at = _request_comment_update(
        comment,
        repository=repository,
        pull_request_number=pull_request_number,
        expected_head=expected_head,
        baseline_time=baseline_time,
        require_match=require_match,
    )
    if updated_at is None:
        return None
    comment_id = _positive_int(comment.get("id"), "Codex request comment ID")
    reactions = api.paginate(f"/repos/{repository}/issues/comments/{comment_id}/reactions")
    not_before = max(baseline_time, updated_at)
    return _clean_codex_reaction(reactions, not_before=not_before)


def _comment_reaction_signal(
    api: RestApi,
    *,
    repository: str,
    pull_request_number: int,
    expected_head: str,
    baseline_time: datetime,
    request_comment_id: int | None,
) -> datetime | None:
    if request_comment_id is not None:
        comment = _record(
            api.get(f"/repos/{repository}/issues/comments/{request_comment_id}"),
            "Codex request comment",
        )
        if _positive_int(comment.get("id"), "Codex request comment ID") != request_comment_id:
            raise ReviewPolicyError("GitHub returned a different Codex request comment")
        return _request_comment_has_reaction(
            api,
            repository=repository,
            pull_request_number=pull_request_number,
            expected_head=expected_head,
            baseline_time=baseline_time,
            comment=comment,
            require_match=True,
        )

    comments = api.paginate(f"/repos/{repository}/issues/{pull_request_number}/comments")
    candidates: list[tuple[datetime, int, dict[str, Any]]] = []
    for comment in comments:
        updated_at = _request_comment_update(
            comment,
            repository=repository,
            pull_request_number=pull_request_number,
            expected_head=expected_head,
            baseline_time=baseline_time,
            require_match=False,
        )
        if updated_at is None:
            continue
        comment_id = _positive_int(comment.get("id"), "Codex request comment ID")
        candidates.append((updated_at, comment_id, comment))
    if len(candidates) > MAX_REQUEST_COMMENT_CANDIDATES:
        raise ReviewPolicyError("too many matching Codex request comments")
    for _updated_at, _comment_id, comment in sorted(candidates, reverse=True):
        signal = _request_comment_has_reaction(
            api,
            repository=repository,
            pull_request_number=pull_request_number,
            expected_head=expected_head,
            baseline_time=baseline_time,
            comment=comment,
            require_match=True,
        )
        if signal is not None:
            return signal
    return None


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
    request_comment_id: int | None = None,
    poll_timeout: float = 600.0,
    poll_interval: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Poll for a fresh, exact-head Codex connector completion signal."""
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
    if action in COMMENT_SIGNAL_ACTIONS:
        if request_comment_id is not None:
            request_comment_id = _positive_int(request_comment_id, "Codex request comment ID")
    elif request_comment_id is not None:
        raise ReviewPolicyError(
            "Codex request comment is only valid for edited, synchronize, or reopened"
        )

    _eligible_pull_request(
        api,
        repository,
        pull_request_number,
        expected_head,
        expected_base_ref,
        expected_base_sha,
    )
    if action == "converted_to_draft":
        raise ReviewPolicyError("converted-to-draft pull requests cannot satisfy review policy")
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
    deadline = monotonic() + poll_timeout
    while True:
        _eligible_pull_request(
            api,
            repository,
            pull_request_number,
            expected_head,
            expected_base_ref,
            expected_base_sha,
        )
        current_reviews = _reviews(api, repository, pull_request_number)
        # GitHub review records bind the head SHA but not the base SHA. Always
        # enforce the frozen event baseline so an edit cannot replay a review
        # from an older same-head candidate after the base advances.
        signal = _codex_review_signal(
            current_reviews,
            expected_head=expected_head,
            not_before=parsed_baseline,
        )
        if not signal and action in ROOT_REACTION_ACTIONS and run_attempt == 1:
            signal = _root_reaction_signal(
                api,
                repository=repository,
                pull_request_number=pull_request_number,
                baseline_time=parsed_baseline,
            )
        if not signal and (
            action in COMMENT_SIGNAL_ACTIONS or action in ROOT_REACTION_ACTIONS and run_attempt > 1
        ):
            signal = _comment_reaction_signal(
                api,
                repository=repository,
                pull_request_number=pull_request_number,
                expected_head=expected_head,
                baseline_time=parsed_baseline,
                request_comment_id=request_comment_id,
            )
        if signal and _confirm_codex_workflow_run_is_current(
            api,
            repository=repository,
            pull_request_number=pull_request_number,
            expected_head=expected_head,
            workflow_run=workflow_run,
        ):
            live_pull_request = _confirm_live_candidate(
                api,
                repository,
                pull_request_number,
                expected_head,
                expected_base_ref,
                expected_base_sha,
            )
            if live_pull_request.updated_at > signal:
                raise ReviewPolicyError(
                    "pull request was updated after the accepted Codex review signal"
                )
            return "eligible exact-head Codex review signal observed"

        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(poll_interval, remaining))
    raise ReviewPolicyError("timed out waiting for a fresh exact-head Codex review signal")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("maintainer", "codex"), required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-base-ref", required=True)
    parser.add_argument("--expected-base-sha", required=True)
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
            ):
                raise ReviewPolicyError("Codex-only arguments cannot be used in maintainer mode")
            result = check_maintainer_policy(api, **common)
        else:
            if (
                args.action is None
                or args.baseline_time is None
                or args.workflow_run_id is None
                or args.run_attempt is None
            ):
                raise ReviewPolicyError(
                    "Codex mode requires --action, --baseline-time, "
                    "--workflow-run-id, and --run-attempt"
                )
            result = check_codex_policy(
                api,
                **common,
                action=args.action,
                baseline_time=args.baseline_time,
                workflow_run_id=args.workflow_run_id,
                run_attempt=args.run_attempt,
                request_comment_id=args.request_comment_id,
                poll_timeout=args.poll_timeout,
                poll_interval=args.poll_interval,
            )
    except ReviewPolicyError as error:
        print(f"review policy failed: {error}", file=sys.stderr)
        return 1
    print(f"review policy passed: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
