"""Fail-closed pull-request review policy tests."""

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
BASELINE = "2026-08-15T19:00:00Z"
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


def _codex_review(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "review_id": 101,
        "user_id": review_policy.CODEX_CONNECTOR_USER_ID,
        "login": review_policy.CODEX_CONNECTOR_LOGIN,
        "state": "COMMENTED",
    }
    values.update(overrides)
    return _review(**values)  # type: ignore[arg-type]


def _reaction(
    *,
    created_at: str = FRESH,
    user_id: int = review_policy.CODEX_CONNECTOR_USER_ID,
    login: str = review_policy.CODEX_CONNECTOR_LOGIN,
    content: str = "+1",
) -> dict[str, object]:
    return {
        "id": 201,
        "user": _actor(user_id, login),
        "content": content,
        "created_at": created_at,
    }


def _request_comment(
    *,
    comment_id: int = 301,
    head: str = HEAD,
    created_at: str = FRESH,
    updated_at: str = FRESH,
    user_id: int = review_policy.MAINTAINER_USER_ID,
    login: str = review_policy.MAINTAINER_LOGIN,
) -> dict[str, object]:
    return {
        "id": comment_id,
        "user": _actor(user_id, login),
        "body": review_policy.codex_request_body(head),
        "issue_url": f"https://api.github.com/repos/{REPOSITORY}/issues/{PULL_REQUEST}",
        "created_at": created_at,
        "updated_at": updated_at,
    }


class FakeApi:
    def __init__(
        self,
        *,
        pull_requests: list[dict[str, object]] | None = None,
        pages: dict[str, list[dict[str, object]]] | None = None,
        records: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.pull_requests = pull_requests or [_pull_request()]
        self.pages = pages or {}
        self.records = records or {}
        self.pull_request_reads = 0
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str) -> object:
        self.calls.append(("get", path))
        if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}":
            index = min(self.pull_request_reads, len(self.pull_requests) - 1)
            self.pull_request_reads += 1
            return self.pull_requests[index]
        try:
            return self.records[path]
        except KeyError as error:
            raise AssertionError(f"unexpected GET: {path}") from error

    def paginate(self, path: str) -> list[dict[str, Any]]:
        self.calls.append(("paginate", path))
        records = self.pages.get(path, [])
        return [dict(record) for record in records]


REVIEWS_PATH = f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}/reviews"
ROOT_REACTIONS_PATH = f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/reactions"
COMMENTS_PATH = f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/comments"


def _maintainer_check(api: FakeApi) -> str:
    return review_policy.check_maintainer_policy(
        api,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        expected_head=HEAD,
        expected_base_ref=BASE_REF,
        expected_base_sha=BASE_SHA,
    )


def _codex_check(
    api: FakeApi,
    *,
    action: str = "opened",
    run_attempt: int = 1,
    base_changed: bool | None = None,
    request_comment_id: int | None = None,
) -> str:
    return review_policy.check_codex_policy(
        api,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        expected_head=HEAD,
        expected_base_ref=BASE_REF,
        expected_base_sha=BASE_SHA,
        action=action,
        baseline_time=BASELINE,
        run_attempt=run_attempt,
        base_changed=base_changed,
        request_comment_id=request_comment_id,
        poll_timeout=0,
        poll_interval=1,
    )


def test_owner_authored_pull_request_does_not_need_self_approval() -> None:
    api = FakeApi(pull_requests=[_pull_request(author_id=review_policy.MAINTAINER_USER_ID)])

    assert "self-approval is not required" in _maintainer_check(api)
    assert api.pull_request_reads == 2
    assert ("paginate", REVIEWS_PATH) not in api.calls


def test_external_author_needs_latest_meaningful_exact_head_approval() -> None:
    approval = _review(
        review_id=1,
        user_id=review_policy.MAINTAINER_USER_ID,
        login=review_policy.MAINTAINER_LOGIN,
        state="APPROVED",
    )
    later_comment = _review(
        review_id=2,
        user_id=review_policy.MAINTAINER_USER_ID,
        login=review_policy.MAINTAINER_LOGIN,
        state="COMMENTED",
        submitted_at=LATER,
    )
    api = FakeApi(pages={REVIEWS_PATH: [approval, later_comment]})

    assert "maintainer approval" in _maintainer_check(api)


@pytest.mark.parametrize("state", ["CHANGES_REQUESTED", "DISMISSED"])
def test_later_negative_meaningful_review_invalidates_approval(state: str) -> None:
    reviews = [
        _review(
            review_id=1,
            user_id=review_policy.MAINTAINER_USER_ID,
            login=review_policy.MAINTAINER_LOGIN,
            state="APPROVED",
        ),
        _review(
            review_id=2,
            user_id=review_policy.MAINTAINER_USER_ID,
            login=review_policy.MAINTAINER_LOGIN,
            state=state,
            submitted_at=LATER,
        ),
    ]

    with pytest.raises(review_policy.ReviewPolicyError, match=state):
        _maintainer_check(FakeApi(pages={REVIEWS_PATH: reviews}))


def test_later_approval_supersedes_changes_requested_on_same_head() -> None:
    reviews = [
        _review(
            review_id=1,
            user_id=review_policy.MAINTAINER_USER_ID,
            login=review_policy.MAINTAINER_LOGIN,
            state="CHANGES_REQUESTED",
        ),
        _review(
            review_id=2,
            user_id=review_policy.MAINTAINER_USER_ID,
            login=review_policy.MAINTAINER_LOGIN,
            state="APPROVED",
            submitted_at=LATER,
        ),
    ]

    assert "maintainer approval" in _maintainer_check(FakeApi(pages={REVIEWS_PATH: reviews}))


def test_stale_head_or_wrong_actor_approval_does_not_pass() -> None:
    reviews = [
        _review(
            review_id=1,
            user_id=review_policy.MAINTAINER_USER_ID,
            login=review_policy.MAINTAINER_LOGIN,
            state="APPROVED",
            head=OTHER_HEAD,
        ),
        _review(
            review_id=2,
            user_id=review_policy.MAINTAINER_USER_ID,
            login="lookalike",
            state="APPROVED",
        ),
    ]

    with pytest.raises(review_policy.ReviewPolicyError, match="exact-head approval"):
        _maintainer_check(FakeApi(pages={REVIEWS_PATH: reviews}))


def test_maintainer_policy_rechecks_live_head_after_approval() -> None:
    approval = _review(
        review_id=1,
        user_id=review_policy.MAINTAINER_USER_ID,
        login=review_policy.MAINTAINER_LOGIN,
        state="APPROVED",
    )
    api = FakeApi(
        pull_requests=[_pull_request(), _pull_request(head=OTHER_HEAD)],
        pages={REVIEWS_PATH: [approval]},
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="live pull request head"):
        _maintainer_check(api)


def test_maintainer_policy_rechecks_live_base_after_approval() -> None:
    approval = _review(
        review_id=1,
        user_id=review_policy.MAINTAINER_USER_ID,
        login=review_policy.MAINTAINER_LOGIN,
        state="APPROVED",
    )
    api = FakeApi(
        pull_requests=[_pull_request(), _pull_request(base_sha=OTHER_BASE_SHA)],
        pages={REVIEWS_PATH: [approval]},
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
def test_every_policy_rejects_ineligible_pull_requests(
    pull_request: dict[str, object], message: str
) -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match=message):
        _maintainer_check(FakeApi(pull_requests=[pull_request]))
    with pytest.raises(review_policy.ReviewPolicyError, match=message):
        _codex_check(FakeApi(pull_requests=[pull_request]))


def test_exact_head_codex_review_that_arrived_before_runner_start_passes() -> None:
    api = FakeApi(pages={REVIEWS_PATH: [_codex_review(review_id=4_944_585_685)]})

    assert "Codex review signal" in _codex_check(api)
    assert api.pull_request_reads == 3


def test_pre_baseline_or_stale_head_codex_review_does_not_pass() -> None:
    reviews = [
        _codex_review(review_id=1, submitted_at="2026-08-15T18:59:59Z"),
        _codex_review(review_id=2, head=OTHER_HEAD),
    ]

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(FakeApi(pages={REVIEWS_PATH: reviews}))


def test_codex_actor_needs_both_immutable_id_and_expected_login() -> None:
    review = _codex_review(login="chatgpt-codex-connector-lookalike")
    reaction = _reaction(login="chatgpt-codex-connector-lookalike")
    api = FakeApi(pages={REVIEWS_PATH: [review], ROOT_REACTIONS_PATH: [reaction]})

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api)


def test_opened_attempt_one_accepts_clean_root_reaction_after_frozen_baseline() -> None:
    api = FakeApi(pages={ROOT_REACTIONS_PATH: [_reaction(created_at=FRESH)]})

    assert "Codex review signal" in _codex_check(api)


def test_opened_rejects_root_reaction_equal_to_frozen_baseline() -> None:
    api = FakeApi(pages={ROOT_REACTIONS_PATH: [_reaction(created_at=BASELINE)]})

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api)


def test_opened_retry_rejects_old_root_reaction() -> None:
    api = FakeApi(pages={ROOT_REACTIONS_PATH: [_reaction(created_at=FRESH)]})

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api, run_attempt=2)
    assert ("paginate", ROOT_REACTIONS_PATH) not in api.calls


def test_opened_retry_accepts_sha_bound_request_comment_reaction() -> None:
    comment = _request_comment()
    comment_reactions = f"/repos/{REPOSITORY}/issues/comments/{comment['id']}/reactions"
    api = FakeApi(
        pages={
            COMMENTS_PATH: [comment],
            comment_reactions: [_reaction(created_at=LATER)],
        }
    )

    assert "Codex review signal" in _codex_check(api, run_attempt=2)


def test_opened_rejects_root_reaction_before_frozen_baseline() -> None:
    api = FakeApi(pages={ROOT_REACTIONS_PATH: [_reaction(created_at="2026-08-15T18:59:59Z")]})

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api)


def test_synchronize_auto_discovers_sha_bound_maintainer_request() -> None:
    comment = _request_comment()
    comment_reactions = f"/repos/{REPOSITORY}/issues/comments/{comment['id']}/reactions"
    api = FakeApi(
        pages={
            COMMENTS_PATH: [comment],
            comment_reactions: [_reaction(created_at=LATER)],
        }
    )

    assert "Codex review signal" in _codex_check(api, action="synchronize")


def test_synchronize_rejects_sha_bound_request_from_wrong_author() -> None:
    comment = _request_comment(user_id=42, login="contributor")
    comment_path = f"/repos/{REPOSITORY}/issues/comments/{comment['id']}"
    api = FakeApi(records={comment_path: comment})

    with pytest.raises(review_policy.ReviewPolicyError, match="authored by the maintainer"):
        _codex_check(
            api,
            action="synchronize",
            request_comment_id=int(comment["id"]),
        )


def test_auto_discovery_ignores_attacker_marker_before_valid_request() -> None:
    attacker_comment = _request_comment(
        comment_id=300,
        user_id=42,
        login="contributor",
    )
    valid_comment = _request_comment(comment_id=301)
    comment_reactions = f"/repos/{REPOSITORY}/issues/comments/{valid_comment['id']}/reactions"
    api = FakeApi(
        pages={
            COMMENTS_PATH: [attacker_comment, valid_comment],
            comment_reactions: [_reaction(created_at=LATER)],
        }
    )

    assert "Codex review signal" in _codex_check(api, action="synchronize")


def test_auto_discovery_ignores_malformed_forged_marker() -> None:
    forged_comment = _request_comment(comment_id=300)
    forged_comment["issue_url"] = "https://api.github.com/repos/attacker/project/issues/1"
    valid_comment = _request_comment(comment_id=301)
    comment_reactions = f"/repos/{REPOSITORY}/issues/comments/{valid_comment['id']}/reactions"
    api = FakeApi(
        pages={
            COMMENTS_PATH: [forged_comment, valid_comment],
            comment_reactions: [_reaction(created_at=LATER)],
        }
    )

    assert "Codex review signal" in _codex_check(api, action="reopened")


def test_auto_discovery_fails_boundedly_before_fetching_excess_reactions() -> None:
    comments = [
        _request_comment(comment_id=1_000 + index)
        for index in range(review_policy.MAX_REQUEST_COMMENT_CANDIDATES + 1)
    ]
    api = FakeApi(pages={COMMENTS_PATH: comments})

    with pytest.raises(review_policy.ReviewPolicyError, match="too many matching"):
        _codex_check(api, action="synchronize")
    comment_reaction_suffix = "/reactions"
    assert not any(
        method == "paginate"
        and "/issues/comments/" in path
        and path.endswith(comment_reaction_suffix)
        for method, path in api.calls
    )


def test_synchronize_rejects_delayed_root_reaction_after_sha_bound_request() -> None:
    comment = _request_comment(updated_at=FRESH)
    api = FakeApi(
        pages={
            COMMENTS_PATH: [comment],
            ROOT_REACTIONS_PATH: [_reaction(created_at=LATER)],
        }
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api, action="synchronize")


def test_synchronize_rejects_root_reaction_before_request() -> None:
    comment = _request_comment(created_at=LATER, updated_at=LATER)
    api = FakeApi(
        pages={
            COMMENTS_PATH: [comment],
            ROOT_REACTIONS_PATH: [_reaction(created_at=FRESH)],
        }
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api, action="synchronize")


def test_final_live_head_check_closes_signal_race() -> None:
    api = FakeApi(
        pull_requests=[_pull_request(), _pull_request(), _pull_request(head=OTHER_HEAD)],
        pages={REVIEWS_PATH: [_codex_review()]},
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="live pull request head"):
        _codex_check(api)


@pytest.mark.parametrize(
    "changed_pull_request",
    [
        _pull_request(base_ref="release"),
        _pull_request(base_sha=OTHER_BASE_SHA),
    ],
)
def test_final_live_base_check_closes_signal_race(
    changed_pull_request: dict[str, object],
) -> None:
    api = FakeApi(
        pull_requests=[_pull_request(), _pull_request(), changed_pull_request],
        pages={REVIEWS_PATH: [_codex_review()]},
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="live pull request base"):
        _codex_check(api)


def test_metadata_only_edit_accepts_prior_exact_head_review() -> None:
    api = FakeApi(pages={REVIEWS_PATH: [_codex_review(submitted_at="2026-08-15T18:00:00Z")]})

    assert "Codex review signal" in _codex_check(
        api,
        action="edited",
        base_changed=False,
    )
    assert api.pull_request_reads == 3


def test_unreviewed_metadata_edit_times_out() -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(FakeApi(), action="edited", base_changed=False)


def test_metadata_edit_does_not_accept_old_root_reaction() -> None:
    api = FakeApi(pages={ROOT_REACTIONS_PATH: [_reaction(created_at="2026-08-15T18:00:00Z")]})

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api, action="edited", base_changed=False)


def test_base_edit_requires_fresh_comment_bound_signal() -> None:
    comment = _request_comment()
    comment_reactions = f"/repos/{REPOSITORY}/issues/comments/{comment['id']}/reactions"
    api = FakeApi(
        pages={
            COMMENTS_PATH: [comment],
            comment_reactions: [_reaction(created_at=LATER)],
        }
    )

    assert "Codex review signal" in _codex_check(
        api,
        action="edited",
        base_changed=True,
    )


def test_base_edit_rejects_pre_edit_exact_head_review() -> None:
    api = FakeApi(pages={REVIEWS_PATH: [_codex_review(submitted_at="2026-08-15T18:00:00Z")]})

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api, action="edited", base_changed=True)


def test_base_edit_rejects_exact_head_review_equal_to_event_baseline() -> None:
    api = FakeApi(pages={REVIEWS_PATH: [_codex_review(submitted_at=BASELINE)]})

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api, action="edited", base_changed=True)


def test_request_comment_must_be_created_strictly_after_event_baseline() -> None:
    comment = _request_comment(created_at=BASELINE, updated_at=FRESH)
    comment_reactions = f"/repos/{REPOSITORY}/issues/comments/{comment['id']}/reactions"
    api = FakeApi(
        pages={
            COMMENTS_PATH: [comment],
            comment_reactions: [_reaction(created_at=LATER)],
        }
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api, action="synchronize")


def test_comment_reaction_must_be_strictly_after_comment_update() -> None:
    comment = _request_comment(created_at=FRESH, updated_at=LATER)
    comment_reactions = f"/repos/{REPOSITORY}/issues/comments/{comment['id']}/reactions"
    api = FakeApi(
        pages={
            COMMENTS_PATH: [comment],
            comment_reactions: [_reaction(created_at=LATER)],
        }
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api, action="synchronize")


def test_edited_requires_base_changed_and_other_actions_reject_it() -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="requires an explicit"):
        _codex_check(FakeApi(), action="edited")
    with pytest.raises(review_policy.ReviewPolicyError, match="only valid for the edited"):
        _codex_check(FakeApi(), action="opened", base_changed=False)


def test_converted_to_draft_action_fails_even_if_api_has_not_caught_up() -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="converted-to-draft"):
        _codex_check(FakeApi(), action="converted_to_draft")


@pytest.mark.parametrize(
    ("timeout", "interval"),
    [(-1, 1), (901, 1), (0, 0), (0, 61)],
)
def test_polling_inputs_are_bounded(timeout: float, interval: float) -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="poll"):
        review_policy.check_codex_policy(
            FakeApi(),
            repository=REPOSITORY,
            pull_request_number=PULL_REQUEST,
            expected_head=HEAD,
            expected_base_ref=BASE_REF,
            expected_base_sha=BASE_SHA,
            action="opened",
            baseline_time=BASELINE,
            run_attempt=1,
            poll_timeout=timeout,
            poll_interval=interval,
        )


@pytest.mark.parametrize("run_attempt", [0, review_policy.MAX_RUN_ATTEMPT + 1])
def test_workflow_run_attempt_is_positive_and_bounded(run_attempt: int) -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="workflow run attempt"):
        _codex_check(FakeApi(), run_attempt=run_attempt)


def test_github_database_ids_are_bounded_at_signed_64_bit() -> None:
    assert review_policy._positive_int(4_944_585_685, "review ID") == 4_944_585_685

    with pytest.raises(review_policy.ReviewPolicyError, match="bounded positive integer"):
        review_policy._positive_int(review_policy.MAX_GITHUB_ID + 1, "review ID")


def test_expected_base_ref_has_a_byte_bound() -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="bounded non-empty"):
        review_policy.check_maintainer_policy(
            FakeApi(),
            repository=REPOSITORY,
            pull_request_number=PULL_REQUEST,
            expected_head=HEAD,
            expected_base_ref="x" * (review_policy.MAX_BASE_REF_BYTES + 1),
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
