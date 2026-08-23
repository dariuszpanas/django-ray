"""Fail-closed maintainer pull-request review policy helpers.

The trusted default-branch publisher uses this bounded GitHub REST client and
policy evaluator without checking out or executing pull-request code.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

MAINTAINER_USER_ID = 15_094_983
MAINTAINER_LOGIN = "dariuszpanas"

API_VERSION = "2022-11-28"
MAX_API_PAGES = 10
MAX_API_RECORDS = 1_000
MAX_API_REQUESTS = 300
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_GITHUB_ID = 9_223_372_036_854_775_807
MAX_BASE_REF_BYTES = 255
MAX_REQUEST_TIMEOUT_SECONDS = 30.0

REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
MEANINGFUL_MAINTAINER_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})


class ReviewPolicyError(RuntimeError):
    """Raised when evidence is missing, stale, malformed, or unsafe."""


class RestApi(Protocol):
    """The small GitHub REST surface used by the maintainer policy."""

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
    base = _record(payload.get("base"), "pull request base")
    author = _record(payload.get("user"), "pull request author")
    head_sha = _commit_sha(head.get("sha"), "pull request head")
    base_ref = _base_ref(base.get("ref"))
    base_sha = _commit_sha(base.get("sha"), "pull request base")
    author_id = _positive_int(author.get("id"), "pull request author ID")
    draft = payload.get("draft")
    state = payload.get("state")
    if not isinstance(draft, bool):
        raise ReviewPolicyError("pull request draft state is invalid")
    if state not in {"open", "closed"}:
        raise ReviewPolicyError("pull request state is invalid")
    return PullRequest(
        number=payload_number,
        head_sha=head_sha,
        base_ref=base_ref,
        base_sha=base_sha,
        author_id=author_id,
        draft=draft,
        state=state,
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
