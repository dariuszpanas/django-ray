"""Fail-closed maintainer pull-request review policy tests."""

from __future__ import annotations

from typing import Any

import pytest

import scripts.check_pr_review_policy as review_policy

REPOSITORY = "dariuszpanas/django-ray"
PULL_REQUEST = 419
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
BASE_REF = "main"
BASE_SHA = "c" * 40
OTHER_BASE_SHA = "d" * 40
FRESH = "2026-08-15T19:00:01Z"
LATER = "2026-08-15T19:00:02Z"


def _actor(user_id: int, login: str) -> dict[str, object]:
    return {"id": user_id, "login": login}


def _pull_request(
    *,
    head: str = HEAD,
    base_ref: str = BASE_REF,
    base_sha: str = BASE_SHA,
    author_id: int = 42,
    draft: bool = False,
    state: str = "open",
) -> dict[str, object]:
    return {
        "number": PULL_REQUEST,
        "head": {"sha": head},
        "base": {"ref": base_ref, "sha": base_sha},
        "user": {"id": author_id, "login": "contributor"},
        "draft": draft,
        "state": state,
    }


def _review(
    *,
    review_id: int,
    user_id: int,
    login: str,
    state: str,
    head: str = HEAD,
    submitted_at: str = FRESH,
) -> dict[str, object]:
    return {
        "id": review_id,
        "user": _actor(user_id, login),
        "state": state,
        "commit_id": head,
        "submitted_at": submitted_at,
    }


class FakeApi:
    def __init__(
        self,
        *,
        pull_requests: list[dict[str, object]] | None = None,
        reviews: list[dict[str, object]] | None = None,
    ) -> None:
        self.pull_requests = pull_requests or [_pull_request()]
        self.reviews = reviews or []
        self.pull_request_reads = 0
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str) -> object:
        self.calls.append(("get", path))
        if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}":
            index = min(self.pull_request_reads, len(self.pull_requests) - 1)
            self.pull_request_reads += 1
            return self.pull_requests[index]
        raise AssertionError(f"unexpected GET: {path}")

    def post(self, path: str, payload: dict[str, object]) -> object:
        raise AssertionError(f"unexpected POST: {path} {payload}")

    def paginate(self, path: str) -> list[dict[str, Any]]:
        self.calls.append(("paginate", path))
        if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}/reviews":
            return [dict(review) for review in self.reviews]
        raise AssertionError(f"unexpected pagination: {path}")


def _maintainer_check(api: FakeApi) -> str:
    return review_policy.check_maintainer_policy(
        api,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        expected_head=HEAD,
        expected_base_ref=BASE_REF,
        expected_base_sha=BASE_SHA,
    )


def _maintainer_review(
    *,
    review_id: int,
    state: str,
    submitted_at: str = FRESH,
    head: str = HEAD,
    login: str = review_policy.MAINTAINER_LOGIN,
) -> dict[str, object]:
    return _review(
        review_id=review_id,
        user_id=review_policy.MAINTAINER_USER_ID,
        login=login,
        state=state,
        head=head,
        submitted_at=submitted_at,
    )


def test_owner_authored_pull_request_does_not_need_self_approval() -> None:
    api = FakeApi(pull_requests=[_pull_request(author_id=review_policy.MAINTAINER_USER_ID)])

    assert "self-approval is not required" in _maintainer_check(api)
    assert api.pull_request_reads == 2
    assert not any(call[0] == "paginate" for call in api.calls)


def test_external_author_needs_latest_meaningful_exact_head_approval() -> None:
    approval = _maintainer_review(review_id=1, state="APPROVED")
    later_comment = _maintainer_review(
        review_id=2,
        state="COMMENTED",
        submitted_at=LATER,
    )
    api = FakeApi(reviews=[approval, later_comment])

    assert "maintainer approval" in _maintainer_check(api)


@pytest.mark.parametrize("state", ["CHANGES_REQUESTED", "DISMISSED"])
def test_later_negative_meaningful_review_invalidates_approval(state: str) -> None:
    reviews = [
        _maintainer_review(review_id=1, state="APPROVED"),
        _maintainer_review(review_id=2, state=state, submitted_at=LATER),
    ]

    with pytest.raises(review_policy.ReviewPolicyError, match=state):
        _maintainer_check(FakeApi(reviews=reviews))


def test_later_approval_supersedes_changes_requested_on_same_head() -> None:
    reviews = [
        _maintainer_review(review_id=1, state="CHANGES_REQUESTED"),
        _maintainer_review(review_id=2, state="APPROVED", submitted_at=LATER),
    ]

    assert "maintainer approval" in _maintainer_check(FakeApi(reviews=reviews))


def test_stale_head_or_wrong_actor_approval_does_not_pass() -> None:
    reviews = [
        _maintainer_review(review_id=1, state="APPROVED", head=OTHER_HEAD),
        _maintainer_review(review_id=2, state="APPROVED", login="lookalike"),
    ]

    with pytest.raises(review_policy.ReviewPolicyError, match="exact-head approval"):
        _maintainer_check(FakeApi(reviews=reviews))


def test_maintainer_policy_rechecks_live_head_after_approval() -> None:
    api = FakeApi(
        pull_requests=[_pull_request(), _pull_request(head=OTHER_HEAD)],
        reviews=[_maintainer_review(review_id=1, state="APPROVED")],
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="live pull request head"):
        _maintainer_check(api)


def test_maintainer_policy_rechecks_live_base_after_approval() -> None:
    api = FakeApi(
        pull_requests=[_pull_request(), _pull_request(base_sha=OTHER_BASE_SHA)],
        reviews=[_maintainer_review(review_id=1, state="APPROVED")],
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="live pull request base"):
        _maintainer_check(api)


@pytest.mark.parametrize(
    ("pull_request", "message"),
    [
        (_pull_request(draft=True), "draft"),
        (_pull_request(head=OTHER_HEAD), "live pull request head"),
        (_pull_request(base_ref="release"), "base ref"),
        (_pull_request(base_sha=OTHER_BASE_SHA), "base SHA"),
        (_pull_request(state="closed"), "not open"),
    ],
)
def test_maintainer_policy_rejects_ineligible_pull_requests(
    pull_request: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match=message):
        _maintainer_check(FakeApi(pull_requests=[pull_request]))


@pytest.mark.parametrize("value", [0, review_policy.MAX_GITHUB_ID + 1, True])
def test_github_database_ids_are_bounded(value: object) -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="bounded positive integer"):
        review_policy._positive_int(value, "test ID")


def test_expected_base_ref_has_a_byte_bound() -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="bounded non-empty"):
        review_policy.check_maintainer_policy(
            FakeApi(),
            repository=REPOSITORY,
            pull_request_number=PULL_REQUEST,
            expected_head=HEAD,
            expected_base_ref="é" * (review_policy.MAX_BASE_REF_BYTES // 2 + 1),
            expected_base_sha=BASE_SHA,
        )


class FakeResponse:
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return b"{}"


def test_rest_client_enforces_one_budget_across_all_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbound_requests = 0

    def open_request(*_args: object, **_kwargs: object) -> FakeResponse:
        nonlocal outbound_requests
        outbound_requests += 1
        return FakeResponse()

    monkeypatch.setattr(review_policy.urllib.request, "urlopen", open_request)
    api = review_policy.GitHubRestApi("test-token")
    api._request_count = review_policy.MAX_API_REQUESTS - 1

    assert api.get("/rate_limit") == {}
    with pytest.raises(review_policy.ReviewPolicyError, match="request budget exhausted"):
        api.get("/rate_limit")
    assert outbound_requests == 1


def test_rest_client_posts_bounded_json(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[Any] = []

    def open_request(request: Any, **_kwargs: object) -> FakeResponse:
        observed.append(request)
        return FakeResponse()

    monkeypatch.setattr(review_policy.urllib.request, "urlopen", open_request)
    api = review_policy.GitHubRestApi("test-token")

    assert api.post("/statuses/example", {"state": "pending"}) == {}
    assert len(observed) == 1
    request = observed[0]
    assert request.method == "POST"
    assert request.data == b'{"state":"pending"}'
    assert request.headers["Content-type"] == "application/json"


class PagingApi(review_policy.GitHubRestApi):
    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self.pages = pages
        self.paths: list[str] = []

    def get(self, path: str) -> object:
        self.paths.append(path)
        return self.pages[len(self.paths) - 1]


def test_rest_pagination_is_bounded_and_requests_explicit_pages() -> None:
    api = PagingApi([[{"id": index} for index in range(100)], [{"id": 101}]])

    assert len(api.paginate("/example")) == 101
    assert api.paths == ["/example?per_page=100&page=1", "/example?per_page=100&page=2"]

    full_pages = [[{"id": index} for index in range(100)] for _ in range(10)]
    with pytest.raises(review_policy.ReviewPolicyError, match="page limit"):
        PagingApi(full_pages).paginate("/example")
