"""Tests for the single-context maintainer approval publisher."""

from __future__ import annotations

from typing import Any

import pytest

from scripts import check_pr_review_policy as review_policy
from scripts import publish_maintainer_approval as publisher

REPOSITORY = "dariuszpanas/django-ray"
PULL_REQUEST = 431
SOURCE_RUN_ID = 10_001
PUBLISHER_RUN_ID = 10_002
PUBLISHER_RUN_ATTEMPT = 2
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
BASE_SHA = "c" * 40
HEAD_BRANCH = "fix/maintainer-approval-state"
STATUS_PATH = f"/repos/{REPOSITORY}/statuses/{HEAD}"
SOURCE_RUN_PATH = f"/repos/{REPOSITORY}/actions/runs/{SOURCE_RUN_ID}"
REVIEWS_PATH = f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}/reviews"


def _actor(user_id: int, login: str) -> dict[str, object]:
    return {"id": user_id, "login": login}


def _pull_request(
    *,
    number: int = PULL_REQUEST,
    head: str = HEAD,
    author_id: int = 123,
) -> dict[str, object]:
    return {
        "number": number,
        "head": {"sha": head, "ref": HEAD_BRANCH},
        "base": {"ref": "main", "sha": BASE_SHA},
        "user": _actor(author_id, "external-author"),
        "draft": False,
        "state": "open",
        "updated_at": "2026-08-20T12:00:00Z",
        "title": "Fix approval publication",
        "body": "Exercise one current-head status.",
    }


def _source_run(
    *,
    event: str = "pull_request_review",
    action: str = "submitted",
    conclusion: str = "success",
    previous_head: str = HEAD,
    pull_request_number: int = PULL_REQUEST,
    head: str = HEAD,
    associated_pull_requests: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if associated_pull_requests is None:
        associated_pull_requests = (
            [
                {
                    "number": PULL_REQUEST,
                    "head": {"sha": HEAD, "ref": HEAD_BRANCH},
                    "base": {"ref": "main", "sha": BASE_SHA},
                }
            ]
            if event == "pull_request_review"
            else []
        )
    return {
        "id": SOURCE_RUN_ID,
        "display_title": (
            f'{{"pr":{pull_request_number}, "head":"{head}", '
            f'"previous":"{previous_head}", "action":"{action}"}}'
        ),
        "event": event,
        "path": publisher.EVENT_WORKFLOW_PATH,
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": HEAD,
        "head_branch": HEAD_BRANCH,
        "pull_requests": associated_pull_requests,
    }


def _review(
    *,
    review_id: int,
    state: str,
    submitted_at: str,
    user_id: int = review_policy.MAINTAINER_USER_ID,
    login: str = review_policy.MAINTAINER_LOGIN,
) -> dict[str, object]:
    return {
        "id": review_id,
        "user": _actor(user_id, login),
        "state": state,
        "commit_id": HEAD,
        "submitted_at": submitted_at,
    }


class FakeApi:
    def __init__(
        self,
        *,
        source_run: dict[str, object] | None = None,
        pull_request: dict[str, object] | None = None,
        review_sequences: list[list[dict[str, object]]] | None = None,
        associated_pull_requests: list[dict[str, object]] | None = None,
        association_sequences: dict[str, list[list[dict[str, object]]]] | None = None,
        existing_statuses: list[dict[str, object]] | None = None,
        pull_requests: dict[int, dict[str, object]] | None = None,
        source_error: bool = False,
        status_error: bool = False,
        malformed_status: bool = False,
    ) -> None:
        self.source_run = source_run or _source_run()
        self.pull_request = pull_request or _pull_request()
        self.review_sequences = review_sequences or [[]]
        self.associated_pull_requests = (
            associated_pull_requests
            if associated_pull_requests is not None
            else [self.pull_request]
        )
        self.association_sequences = association_sequences or {}
        self.association_reads: dict[str, int] = {}
        self.existing_statuses = existing_statuses or []
        self.pull_requests = pull_requests or {int(self.pull_request["number"]): self.pull_request}
        self.source_error = source_error
        self.status_error = status_error
        self.malformed_status = malformed_status
        self.review_reads = 0
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.call_order: list[str] = []

    def get(self, path: str) -> object:
        if path == SOURCE_RUN_PATH:
            if self.source_error:
                raise review_policy.ReviewPolicyError("source workflow lookup failed")
            return self.source_run
        pull_prefix = f"/repos/{REPOSITORY}/pulls/"
        if path.startswith(pull_prefix):
            number = int(path.removeprefix(pull_prefix))
            return self.pull_requests[number]
        raise AssertionError(f"unexpected GET: {path}")

    def post(self, path: str, payload: dict[str, object]) -> object:
        self.call_order.append("status-post")
        self.posts.append((path, dict(payload)))
        if self.malformed_status:
            return {"id": len(self.posts), "state": payload["state"], "context": "wrong"}
        return {
            "id": len(self.posts),
            "state": payload["state"],
            "context": payload["context"],
        }

    def paginate(self, path: str) -> list[dict[str, Any]]:
        status_list_prefix = f"/repos/{REPOSITORY}/commits/"
        if path.startswith(status_list_prefix) and path.endswith("/statuses"):
            self.call_order.append("status-list")
            if self.status_error:
                raise review_policy.ReviewPolicyError("status pagination exceeded the record limit")
            head_sha = path.removeprefix(status_list_prefix).removesuffix("/statuses")
            posted_statuses = [
                {"id": 1_000_000 + index, **payload}
                for index, (posted_path, payload) in enumerate(self.posts, start=1)
                if posted_path == f"/repos/{REPOSITORY}/statuses/{head_sha}"
            ]
            existing_statuses = self.existing_statuses if head_sha == HEAD else []
            return [*existing_statuses, *posted_statuses]
        association_prefix = f"/repos/{REPOSITORY}/commits/"
        if path.startswith(association_prefix) and path.endswith("/pulls"):
            self.call_order.append("association")
            head_sha = path.removeprefix(association_prefix).removesuffix("/pulls")
            sequences = self.association_sequences.get(head_sha)
            if sequences is not None:
                index = min(self.association_reads.get(head_sha, 0), len(sequences) - 1)
                self.association_reads[head_sha] = index + 1
                return [dict(record) for record in sequences[index]]
            return [dict(record) for record in self.associated_pull_requests]
        if path == REVIEWS_PATH:
            self.call_order.append("reviews")
            index = min(self.review_reads, len(self.review_sequences) - 1)
            self.review_reads += 1
            return [dict(record) for record in self.review_sequences[index]]
        raise AssertionError(f"unexpected pagination: {path}")


def _publish(
    api: FakeApi,
    *,
    expected_head: str = HEAD,
    previous_head: str = HEAD,
    source_action: str = publisher.REVIEW_SOURCE_ACTION,
    candidate_head: str = HEAD,
) -> str:
    return publisher.publish_maintainer_approval(
        api,
        repository=REPOSITORY,
        source_workflow_run_id=SOURCE_RUN_ID,
        publisher_workflow_run_id=PUBLISHER_RUN_ID,
        publisher_workflow_run_attempt=PUBLISHER_RUN_ATTEMPT,
        server_url="https://github.com",
        expected_head=expected_head,
        expected_previous_head=previous_head,
        expected_source_action=source_action,
        candidate_head=candidate_head,
    )


def _states(api: FakeApi) -> list[str]:
    return [str(payload["state"]) for _path, payload in api.posts]


def test_external_pull_request_without_approval_replaces_one_status_context() -> None:
    api = FakeApi()

    assert _publish(api).startswith("failure for")
    assert _states(api) == ["failure"]
    assert {path for path, _payload in api.posts} == {STATUS_PATH}
    assert {payload["context"] for _path, payload in api.posts} == {publisher.STATUS_CONTEXT}
    assert all(
        payload["target_url"]
        == (
            f"https://github.com/{REPOSITORY}/actions/runs/{PUBLISHER_RUN_ID}"
            f"/attempts/{PUBLISHER_RUN_ATTEMPT}"
        )
        for _path, payload in api.posts
    )


def test_owner_authored_pull_request_publishes_success_without_review() -> None:
    api = FakeApi(pull_request=_pull_request(author_id=review_policy.MAINTAINER_USER_ID))

    assert _publish(api).startswith("success for")
    assert _states(api) == ["success"]


def test_approval_dismissal_and_reapproval_update_the_same_context() -> None:
    approved = _review(
        review_id=1,
        state="APPROVED",
        submitted_at="2026-08-20T12:01:00Z",
    )
    dismissed = _review(
        review_id=2,
        state="DISMISSED",
        submitted_at="2026-08-20T12:02:00Z",
    )
    reapproved = _review(
        review_id=3,
        state="APPROVED",
        submitted_at="2026-08-20T12:03:00Z",
    )
    api = FakeApi(
        review_sequences=[
            [approved],
            [approved],
            [approved],
            [approved, dismissed],
            [approved, dismissed],
            [approved, dismissed, reapproved],
            [approved, dismissed, reapproved],
            [approved, dismissed, reapproved],
        ]
    )

    assert _publish(api).startswith("success for")
    assert _publish(api).startswith("failure for")
    assert _publish(api).startswith("success for")
    assert _states(api) == [
        "success",
        "failure",
        "success",
    ]
    assert {payload["context"] for _path, payload in api.posts} == {publisher.STATUS_CONTEXT}


def test_irrelevant_commented_review_does_not_override_maintainer_approval() -> None:
    reviews = [
        _review(
            review_id=1,
            state="APPROVED",
            submitted_at="2026-08-20T12:01:00Z",
        ),
        _review(
            review_id=2,
            state="COMMENTED",
            submitted_at="2026-08-20T12:02:00Z",
            user_id=review_policy.CODEX_CONNECTOR_USER_ID,
            login=review_policy.CODEX_CONNECTOR_LOGIN,
        ),
    ]
    api = FakeApi(review_sequences=[reviews])

    assert _publish(api).startswith("success for")
    assert _states(api) == ["success"]


def test_approval_is_rechecked_after_capacity_and_before_final_ownership() -> None:
    approved = _review(
        review_id=1,
        state="APPROVED",
        submitted_at="2026-08-20T12:01:00Z",
    )
    dismissed = _review(
        review_id=2,
        state="DISMISSED",
        submitted_at="2026-08-20T12:02:00Z",
    )
    api = FakeApi(review_sequences=[[approved], [approved], [approved, dismissed]])

    assert _publish(api).startswith("failure for")
    assert _states(api) == ["failure"]
    assert api.call_order == [
        "association",
        "reviews",
        "association",
        "reviews",
        "association",
        "status-list",
        "reviews",
        "association",
        "status-post",
    ]


def test_approval_added_during_evaluation_can_publish_the_fresh_state() -> None:
    approved = _review(
        review_id=1,
        state="APPROVED",
        submitted_at="2026-08-20T12:01:00Z",
    )
    api = FakeApi(review_sequences=[[], [approved]])

    assert _publish(api).startswith("success for")
    assert _states(api) == ["success"]


def test_failed_event_workflow_still_publishes_the_live_policy_state() -> None:
    api = FakeApi(
        source_run=_source_run(conclusion="failure"),
        pull_request=_pull_request(author_id=review_policy.MAINTAINER_USER_ID),
    )

    assert _publish(api).startswith("success for")
    assert _states(api) == ["success"]
    assert api.review_reads == 0


def test_stale_source_head_does_not_publish_to_the_new_head() -> None:
    current_pull_request = _pull_request(head=OTHER_HEAD)
    api = FakeApi(
        pull_request=current_pull_request,
        associated_pull_requests=[current_pull_request],
    )

    assert _publish(api).startswith("success for")
    assert _states(api) == ["success"]
    assert {path for path, _payload in api.posts} == {STATUS_PATH}


def test_review_source_with_no_live_association_remains_pending() -> None:
    api = FakeApi(associated_pull_requests=[])

    assert _publish(api).startswith("pending for")
    assert _states(api) == []


def test_source_lookup_failure_does_not_publish_a_final_state() -> None:
    api = FakeApi(source_error=True)

    with pytest.raises(review_policy.ReviewPolicyError, match="source workflow lookup failed"):
        _publish(api)
    assert _states(api) == []


def test_shared_head_cannot_publish_success_for_two_open_pull_requests() -> None:
    second_pull_request = _pull_request(number=PULL_REQUEST + 1)
    api = FakeApi(associated_pull_requests=[_pull_request(), second_pull_request])

    assert "exactly one open" in _publish(api)
    assert _states(api) == []
    assert api.review_reads == 0


def test_success_rechecks_unique_head_ownership_immediately_before_publication() -> None:
    pull_request = _pull_request(author_id=review_policy.MAINTAINER_USER_ID)
    second_pull_request = _pull_request(number=PULL_REQUEST + 1)
    api = FakeApi(
        pull_request=pull_request,
        association_sequences={HEAD: [[pull_request], [pull_request, second_pull_request]]},
    )

    assert "exactly one open" in _publish(api)
    assert _states(api) == []


def test_final_publication_rechecks_ownership_after_the_live_policy() -> None:
    pull_request = _pull_request(author_id=review_policy.MAINTAINER_USER_ID)
    second_pull_request = _pull_request(number=PULL_REQUEST + 1)
    api = FakeApi(
        pull_request=pull_request,
        association_sequences={
            HEAD: [
                [pull_request],
                [pull_request],
                [pull_request],
                [pull_request, second_pull_request],
            ]
        },
    )

    assert "exactly one open" in _publish(api)
    assert _states(api) == []


def test_failure_publication_rechecks_ownership_after_the_live_policy() -> None:
    pull_request = _pull_request()
    second_pull_request = _pull_request(number=PULL_REQUEST + 1)
    api = FakeApi(
        association_sequences={
            HEAD: [
                [pull_request],
                [pull_request],
                [pull_request, second_pull_request],
            ]
        },
    )

    assert "exactly one open" in _publish(api)
    assert _states(api) == []
    assert api.review_reads == 2


def test_status_capacity_uses_the_last_slot_to_fail_closed() -> None:
    existing_statuses = [
        {
            "id": index,
            "context": publisher.STATUS_CONTEXT,
            "state": "success",
            "target_url": "https://github.com/example/older-run",
        }
        for index in range(1, publisher.MAX_STATUSES_PER_CONTEXT - 1)
    ]
    api = FakeApi(
        pull_request=_pull_request(author_id=review_policy.MAINTAINER_USER_ID),
        existing_statuses=existing_statuses,
    )

    assert "capacity is exhausted" in _publish(api)
    assert _states(api) == ["failure"]


def test_exhausted_status_capacity_does_not_attempt_an_over_limit_post() -> None:
    existing_statuses = [
        {
            "id": index,
            "context": publisher.STATUS_CONTEXT,
            "state": "success",
            "target_url": "https://github.com/example/older-run",
        }
        for index in range(1, publisher.MAX_STATUSES_PER_CONTEXT)
    ]
    api = FakeApi(
        pull_request=_pull_request(author_id=review_policy.MAINTAINER_USER_ID),
        existing_statuses=existing_statuses,
    )

    assert _publish(api).startswith("pending for")
    assert _states(api) == []


def test_rerun_pending_from_an_older_attempt_does_not_hide_the_new_invalidation() -> None:
    existing_statuses = [
        {
            "id": index,
            "context": publisher.STATUS_CONTEXT,
            "state": "success",
            "target_url": "https://github.com/example/older-run",
        }
        for index in range(1, publisher.MAX_STATUSES_PER_CONTEXT - 1)
    ]
    existing_statuses[0] = {
        "id": 1,
        "context": publisher.STATUS_CONTEXT,
        "state": "pending",
        "target_url": (
            f"https://github.com/{REPOSITORY}/actions/runs/{PUBLISHER_RUN_ID}/attempts/1"
        ),
    }
    api = FakeApi(
        pull_request=_pull_request(author_id=review_policy.MAINTAINER_USER_ID),
        existing_statuses=existing_statuses,
    )

    assert "capacity is exhausted" in _publish(api)
    assert _states(api) == ["failure"]


def test_associated_pull_request_with_a_later_head_is_not_shared_ownership() -> None:
    pull_request = _pull_request(author_id=review_policy.MAINTAINER_USER_ID)
    stacked_pull_request = _pull_request(number=PULL_REQUEST + 1, head=OTHER_HEAD)
    api = FakeApi(
        pull_request=pull_request,
        associated_pull_requests=[pull_request, stacked_pull_request],
    )

    assert _publish(api).startswith("success for")
    assert _states(api) == ["success"]


def test_synchronize_rechecks_the_previous_head_for_the_remaining_pull_request() -> None:
    moved_pull_request = _pull_request(author_id=review_policy.MAINTAINER_USER_ID)
    remaining_pull_request = _pull_request(
        number=PULL_REQUEST + 1,
        head=OTHER_HEAD,
        author_id=review_policy.MAINTAINER_USER_ID,
    )
    api = FakeApi(
        source_run=_source_run(
            event="pull_request_target",
            action="synchronize",
            previous_head=OTHER_HEAD,
        ),
        pull_request=moved_pull_request,
        association_sequences={
            HEAD: [[moved_pull_request]],
            OTHER_HEAD: [[remaining_pull_request]],
        },
        pull_requests={
            PULL_REQUEST: moved_pull_request,
            PULL_REQUEST + 1: remaining_pull_request,
        },
    )

    current_result = _publish(
        api,
        previous_head=OTHER_HEAD,
        source_action="synchronize",
    )
    previous_result = _publish(
        api,
        previous_head=OTHER_HEAD,
        source_action="synchronize",
        candidate_head=OTHER_HEAD,
    )

    assert current_result.startswith("success for")
    assert previous_result.startswith("success for")
    assert _states(api) == ["success", "success"]
    assert {path for path, _payload in api.posts} == {
        STATUS_PATH,
        f"/repos/{REPOSITORY}/statuses/{OTHER_HEAD}",
    }


def test_closed_shared_head_rechecks_the_remaining_pull_request() -> None:
    closed_pull_request = _pull_request()
    closed_pull_request["state"] = "closed"
    remaining_pull_request = _pull_request(
        number=PULL_REQUEST + 1,
        author_id=review_policy.MAINTAINER_USER_ID,
    )
    api = FakeApi(
        source_run=_source_run(event="pull_request_target", action="closed"),
        pull_request=closed_pull_request,
        associated_pull_requests=[closed_pull_request, remaining_pull_request],
        pull_requests={
            PULL_REQUEST: closed_pull_request,
            PULL_REQUEST + 1: remaining_pull_request,
        },
    )

    assert _publish(api, source_action="closed").startswith("success for")
    assert _states(api) == ["success"]


def test_closed_head_without_an_owner_restores_a_terminal_status() -> None:
    closed_pull_request = _pull_request()
    closed_pull_request["state"] = "closed"
    api = FakeApi(
        source_run=_source_run(event="pull_request_target", action="closed"),
        pull_request=closed_pull_request,
        associated_pull_requests=[closed_pull_request],
    )

    result = _publish(api, source_action="closed")

    assert result.startswith("success for")
    assert "no open pull request" in result
    assert _states(api) == ["success"]
    assert api.call_order == [
        "association",
        "status-list",
        "association",
        "status-post",
    ]


@pytest.mark.parametrize(
    ("event", "action", "source_action"),
    [
        ("pull_request_review", "submitted", publisher.REVIEW_SOURCE_ACTION),
        ("pull_request_target", "synchronize", "synchronize"),
    ],
)
def test_delayed_older_source_cannot_leave_a_closed_head_pending(
    event: str,
    action: str,
    source_action: str,
) -> None:
    closed_pull_request = _pull_request()
    closed_pull_request["state"] = "closed"
    api = FakeApi(
        source_run=_source_run(event="pull_request_target", action="closed"),
        pull_request=closed_pull_request,
        associated_pull_requests=[closed_pull_request],
    )

    close_result = _publish(api, source_action="closed")
    api.source_run = _source_run(event=event, action=action)
    delayed_result = _publish(api, source_action=source_action)

    assert close_result.startswith("success for")
    assert delayed_result.startswith("success for")
    assert _states(api) == ["success", "success"]


def test_closed_recovery_status_scan_failure_cannot_publish_a_final_state() -> None:
    closed_pull_request = _pull_request()
    closed_pull_request["state"] = "closed"
    remaining_pull_request = _pull_request(
        number=PULL_REQUEST + 1,
        author_id=review_policy.MAINTAINER_USER_ID,
    )
    api = FakeApi(
        source_run=_source_run(event="pull_request_target", action="closed"),
        pull_request=closed_pull_request,
        associated_pull_requests=[closed_pull_request, remaining_pull_request],
        pull_requests={
            PULL_REQUEST: closed_pull_request,
            PULL_REQUEST + 1: remaining_pull_request,
        },
        status_error=True,
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="pagination exceeded"):
        _publish(api, source_action="closed")
    assert _states(api) == []


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"path": ".github/workflows/other.yml"}, "unexpected path"),
        ({"event": "push"}, "unexpected event"),
        ({"status": "in_progress"}, "not complete"),
    ],
)
def test_source_workflow_identity_fails_closed(override: dict[str, object], message: str) -> None:
    source_run = _source_run()
    source_run.update(override)

    with pytest.raises(review_policy.ReviewPolicyError, match=message):
        api = FakeApi(source_run=source_run)
        _publish(api)
    assert _states(api) == []


@pytest.mark.parametrize(
    "associated_pull_requests",
    [
        [],
        [
            {
                "number": PULL_REQUEST,
                "head": {"sha": HEAD},
            },
            {
                "number": PULL_REQUEST + 1,
                "head": {"sha": HEAD},
            },
        ],
    ],
)
def test_review_source_does_not_require_a_stable_github_pr_association(
    associated_pull_requests: list[dict[str, object]],
) -> None:
    api = FakeApi(
        source_run=_source_run(associated_pull_requests=associated_pull_requests),
        pull_request=_pull_request(author_id=review_policy.MAINTAINER_USER_ID),
    )

    assert _publish(api).startswith("success for")
    assert _states(api) == ["success"]


def test_review_source_ignores_untrusted_title_markers() -> None:
    source_run = _source_run(
        action="synchronize",
        pull_request_number=PULL_REQUEST + 1,
        head=OTHER_HEAD,
        previous_head=OTHER_HEAD,
    )
    source_run["display_title"] = "untrusted pull-request workflow title"
    api = FakeApi(
        source_run=source_run,
        pull_request=_pull_request(author_id=review_policy.MAINTAINER_USER_ID),
    )

    assert _publish(api).startswith("success for")
    assert _states(api) == ["success"]
    assert {path for path, _payload in api.posts} == {STATUS_PATH}


def test_review_source_uses_the_github_run_head() -> None:
    source_run = _source_run()
    source_run["head_sha"] = OTHER_HEAD
    api = FakeApi(source_run=source_run)

    with pytest.raises(review_policy.ReviewPolicyError, match="unexpected head"):
        _publish(api)
    assert _states(api) == []


def test_malformed_status_response_fails_closed() -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="different maintainer"):
        _publish(FakeApi(malformed_status=True))


def test_pull_request_target_base_sha_does_not_replace_the_marked_pr_head() -> None:
    source_run = _source_run(event="pull_request_target", action="opened")
    source_run.update({"head_sha": BASE_SHA, "head_branch": "main"})
    api = FakeApi(
        source_run=source_run,
        pull_request=_pull_request(author_id=review_policy.MAINTAINER_USER_ID),
    )

    assert _publish(api, source_action="opened").startswith("success for")
    assert _states(api) == ["success"]
