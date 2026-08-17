"""Fail-closed pull-request review policy tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
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
TITLE = "refactor: organize package modules"
BODY = "Keep behavior stable while improving package structure."
METADATA_DIGEST = review_policy._pull_request_metadata_digest(TITLE, BODY)
EMPTY_LIFECYCLE_DIGEST = hashlib.sha256(b"[]").hexdigest()
PRIOR_BASELINE = "2026-08-15T18:00:00Z"
BASELINE = "2026-08-15T19:00:00Z"
FRESH = "2026-08-15T19:00:01Z"
LATER = "2026-08-15T19:00:02Z"
LATEST = "2026-08-15T19:00:03Z"
WORKFLOW_RUN_ID = 31_915_185_903
WORKFLOW_RUN_NUMBER = 3


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
    updated_at: str = BASELINE,
    title: object = TITLE,
    body: object = BODY,
) -> dict[str, object]:
    return {
        "number": PULL_REQUEST,
        "head": {"sha": head},
        "base": {"ref": base_ref, "sha": base_sha},
        "user": {"id": author_id, "login": "contributor"},
        "draft": draft,
        "state": state,
        "updated_at": updated_at,
        "title": title,
        "body": body,
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
        "submitted_at": LATER,
    }
    values.update(overrides)
    return _review(**values)  # type: ignore[arg-type]


def _reaction(
    *,
    reaction_id: int = 201,
    created_at: str = FRESH,
    user_id: int = review_policy.CODEX_CONNECTOR_USER_ID,
    login: str = review_policy.CODEX_CONNECTOR_LOGIN,
    content: str = "+1",
) -> dict[str, object]:
    return {
        "id": reaction_id,
        "user": _actor(user_id, login),
        "content": content,
        "created_at": created_at,
    }


def _issue_event(
    *,
    event_id: int = 501,
    event: object = "closed",
    created_at: object = PRIOR_BASELINE,
) -> dict[str, object]:
    return {
        "id": event_id,
        "event": event,
        "created_at": created_at,
    }


def _expected_lifecycle_digest(events: list[dict[str, object]]) -> str:
    canonical = [
        (
            int(event["id"]),
            str(event["event"]),
            review_policy._timestamp(event["created_at"], "test lifecycle event").isoformat(),
        )
        for event in events
    ]
    canonical.sort(key=lambda item: (item[2], item[0], item[1]))
    encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _lifecycle_digest(
    api: review_policy.RestApi,
    *,
    action: str = "opened",
) -> str:
    return review_policy._pull_request_lifecycle_digest(
        api,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        action=action,
        baseline_time=review_policy._timestamp(BASELINE, "test event baseline"),
    )


def _request_comment(
    *,
    comment_id: int = 301,
    head: str = HEAD,
    created_at: str = FRESH,
    updated_at: str = FRESH,
    user_id: int = review_policy.GITHUB_ACTIONS_USER_ID,
    login: str = review_policy.GITHUB_ACTIONS_LOGIN,
    workflow_run_id: int = WORKFLOW_RUN_ID,
    run_attempt: int = 1,
    metadata_digest: str = METADATA_DIGEST,
    lifecycle_digest: str = EMPTY_LIFECYCLE_DIGEST,
) -> dict[str, object]:
    return {
        "id": comment_id,
        "user": _actor(user_id, login),
        "body": review_policy.codex_request_body(
            head,
            workflow_run_id=workflow_run_id,
            run_attempt=run_attempt,
            metadata_digest=metadata_digest,
            lifecycle_digest=lifecycle_digest,
        ),
        "issue_url": f"https://api.github.com/repos/{REPOSITORY}/issues/{PULL_REQUEST}",
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _workflow_run(
    *,
    run_id: int = WORKFLOW_RUN_ID,
    run_number: int = WORKFLOW_RUN_NUMBER,
    run_attempt: int = 1,
    pull_request_number: int = PULL_REQUEST,
    head: str = HEAD,
    base_ref: str = BASE_REF,
    base_sha: str = BASE_SHA,
    event: str = "pull_request_target",
    path: str = review_policy.CODEX_WORKFLOW_PATH,
    created_at: str = BASELINE,
    run_started_at: str | None = None,
) -> dict[str, object]:
    return {
        "id": run_id,
        "run_number": run_number,
        "run_attempt": run_attempt,
        "event": event,
        "path": path,
        "head_sha": head,
        "created_at": created_at,
        "run_started_at": run_started_at or created_at,
        "pull_requests": [
            {
                "number": pull_request_number,
                "head": {"sha": head},
                "base": {"ref": base_ref, "sha": base_sha},
            }
        ],
    }


DEFAULT_REQUEST_COMMENT_ID = 301
REQUEST_COMMENT_PATH = f"/repos/{REPOSITORY}/issues/comments/{DEFAULT_REQUEST_COMMENT_ID}"
CURRENT_WORKFLOW_RUN_PATH = f"/repos/{REPOSITORY}/actions/runs/{WORKFLOW_RUN_ID}"
WORKFLOW_RUNS_QUERY = review_policy.urllib.parse.urlencode(
    {
        "event": "pull_request_target",
        "head_sha": HEAD,
        "created": f">={BASELINE}",
        "per_page": str(review_policy.MAX_WORKFLOW_RUN_RECORDS),
    }
)
WORKFLOW_RUNS_PATH = (
    f"/repos/{REPOSITORY}/actions/workflows/{review_policy.CODEX_WORKFLOW_FILE}/runs"
    f"?{WORKFLOW_RUNS_QUERY}"
)
LIFECYCLE_EVENTS_PATH = f"/repos/{REPOSITORY}/issues/{PULL_REQUEST}/events"


class FakeApi:
    def __init__(
        self,
        *,
        pull_requests: list[dict[str, object]] | None = None,
        pages: dict[str, list[dict[str, object]]] | None = None,
        records: dict[str, object] | None = None,
        workflow_run: dict[str, object] | None = None,
        record_sequences: dict[str, list[object]] | None = None,
        page_sequences: dict[str, list[list[dict[str, object]]]] | None = None,
        post_records: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.pull_requests = pull_requests or [_pull_request()]
        self.pages = pages or {}
        workflow_run = workflow_run or _workflow_run()
        request_run_id = int(workflow_run["id"])
        request_run_attempt = int(workflow_run["run_attempt"])
        self.records: dict[str, object] = {
            CURRENT_WORKFLOW_RUN_PATH: workflow_run,
            WORKFLOW_RUNS_PATH: {
                "total_count": 1,
                "workflow_runs": [workflow_run],
            },
            REQUEST_COMMENT_PATH: _request_comment(
                workflow_run_id=request_run_id,
                run_attempt=request_run_attempt,
            ),
        }
        if records:
            self.records.update(records)
        self.record_sequences = record_sequences or {}
        self.page_sequences = page_sequences or {}
        self.post_records = post_records or {}
        self.record_reads: dict[str, int] = {}
        self.page_reads: dict[str, int] = {}
        self.pull_request_reads = 0
        self.calls: list[tuple[str, str]] = []
        self.posts: list[tuple[str, dict[str, object]]] = []

    def get(self, path: str) -> object:
        self.calls.append(("get", path))
        if path == f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}":
            index = min(self.pull_request_reads, len(self.pull_requests) - 1)
            self.pull_request_reads += 1
            return self.pull_requests[index]
        if path in self.record_sequences:
            sequence = self.record_sequences[path]
            index = min(self.record_reads.get(path, 0), len(sequence) - 1)
            self.record_reads[path] = index + 1
            return sequence[index]
        if path in self.records:
            return self.records[path]
        if "?content=eyes&per_page=" in path:
            return []
        comment_prefix = f"/repos/{REPOSITORY}/issues/comments/"
        if path.startswith(comment_prefix):
            comment_id = int(path.removeprefix(comment_prefix))
            for comment in self.pages.get(COMMENTS_PATH, []):
                if comment.get("id") == comment_id:
                    return comment
        raise AssertionError(f"unexpected GET: {path}")

    def post(self, path: str, payload: dict[str, object]) -> object:
        self.calls.append(("post", path))
        self.posts.append((path, dict(payload)))
        if path in self.post_records:
            return self.post_records[path]
        raise AssertionError(f"unexpected POST: {path}")

    def paginate(self, path: str) -> list[dict[str, Any]]:
        self.calls.append(("paginate", path))
        if path in self.page_sequences:
            sequence = self.page_sequences[path]
            if not sequence:
                records: list[dict[str, object]] = []
            else:
                read_count = self.page_reads.get(path, 0)
                records = sequence[min(read_count, len(sequence) - 1)]
                self.page_reads[path] = read_count + 1
        else:
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
    baseline_time: str = BASELINE,
    expected_base_sha: str = BASE_SHA,
    workflow_run_id: int = WORKFLOW_RUN_ID,
    run_attempt: int = 1,
    expected_title: str = TITLE,
    expected_body: str = BODY,
    request_comment_id: int | None = DEFAULT_REQUEST_COMMENT_ID,
) -> str:
    return review_policy.check_codex_policy(
        api,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        expected_head=HEAD,
        expected_base_ref=BASE_REF,
        expected_base_sha=expected_base_sha,
        action=action,
        baseline_time=baseline_time,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        expected_title=expected_title,
        expected_body=expected_body,
        request_comment_id=request_comment_id,
        poll_timeout=0,
        poll_interval=1,
    )


def _codex_poll_check(
    api: FakeApi,
    *,
    action: str = "opened",
    workflow_run_id: int = WORKFLOW_RUN_ID,
    run_attempt: int = 1,
    expected_title: str = TITLE,
    expected_body: str = BODY,
) -> str:
    clock = iter([0.0, 0.0, 1.0])
    return review_policy.check_codex_policy(
        api,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        expected_head=HEAD,
        expected_base_ref=BASE_REF,
        expected_base_sha=BASE_SHA,
        action=action,
        baseline_time=BASELINE,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        expected_title=expected_title,
        expected_body=expected_body,
        request_comment_id=DEFAULT_REQUEST_COMMENT_ID,
        poll_timeout=1,
        poll_interval=1,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )


def _ensure_request(
    api: FakeApi,
    *,
    action: str = "synchronize",
    baseline_time: str = BASELINE,
    run_attempt: int = 1,
    expected_title: str = TITLE,
    expected_body: str = BODY,
) -> int:
    return review_policy.ensure_codex_request_comment(
        api,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        expected_head=HEAD,
        expected_base_ref=BASE_REF,
        expected_base_sha=BASE_SHA,
        action=action,
        baseline_time=baseline_time,
        workflow_run_id=WORKFLOW_RUN_ID,
        run_attempt=run_attempt,
        expected_title=expected_title,
        expected_body=expected_body,
    )


def _write_event_file(
    directory: Path,
    *,
    title: object = TITLE,
    body: object = BODY,
) -> Path:
    event_path = directory / "event.json"
    event_path.write_text(
        json.dumps({"pull_request": {"title": title, "body": body}}),
        encoding="utf-8",
    )
    return event_path


def _codex_request_cli_args(event_path: Path) -> list[str]:
    return [
        "--mode",
        "codex-request",
        "--repository",
        REPOSITORY,
        "--pull-request",
        str(PULL_REQUEST),
        "--expected-head",
        HEAD,
        "--expected-base-ref",
        BASE_REF,
        "--expected-base-sha",
        BASE_SHA,
        "--event-path",
        str(event_path),
        "--action",
        "synchronize",
        "--baseline-time",
        BASELINE,
        "--workflow-run-id",
        str(WORKFLOW_RUN_ID),
        "--run-attempt",
        "1",
    ]


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


@pytest.mark.parametrize(
    ("title", "body", "message"),
    [
        (None, BODY, "title and body must be strings"),
        (42, BODY, "title and body must be strings"),
        (TITLE, 42, "title and body must be strings"),
        ("", BODY, "title exceeds its byte limit"),
        ("x" * (review_policy.MAX_PULL_REQUEST_TITLE_BYTES + 1), BODY, "title exceeds"),
        (TITLE, "x" * (review_policy.MAX_PULL_REQUEST_BODY_BYTES + 1), "body exceeds"),
    ],
    ids=(
        "missing-title",
        "invalid-title",
        "invalid-body",
        "empty-title",
        "oversize-title",
        "oversize-body",
    ),
)
def test_pull_request_metadata_is_required_and_bounded(
    title: object,
    body: object,
    message: str,
) -> None:
    api = FakeApi(pull_requests=[_pull_request(title=title, body=body)])

    with pytest.raises(review_policy.ReviewPolicyError, match=message):
        _codex_check(api)


@pytest.mark.parametrize(
    "metadata_digest",
    ["", "a" * 63, "g" * 64, "a" * 65],
)
def test_request_marker_rejects_missing_invalid_or_oversize_metadata_digest(
    metadata_digest: str,
) -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="metadata digest is invalid"):
        review_policy.codex_request_body(
            HEAD,
            workflow_run_id=WORKFLOW_RUN_ID,
            run_attempt=1,
            metadata_digest=metadata_digest,
            lifecycle_digest=EMPTY_LIFECYCLE_DIGEST,
        )


@pytest.mark.parametrize(
    "lifecycle_digest",
    ["", "a" * 63, "g" * 64, "a" * 65],
)
def test_request_marker_rejects_missing_invalid_or_oversize_lifecycle_digest(
    lifecycle_digest: str,
) -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="lifecycle digest is invalid"):
        review_policy.codex_request_body(
            HEAD,
            workflow_run_id=WORKFLOW_RUN_ID,
            run_attempt=1,
            metadata_digest=METADATA_DIGEST,
            lifecycle_digest=lifecycle_digest,
        )


def test_workflow_creates_one_exact_head_codex_request() -> None:
    created_comment = _request_comment(comment_id=401)
    api = FakeApi(
        pages={COMMENTS_PATH: []},
        post_records={COMMENTS_PATH: created_comment},
    )

    assert _ensure_request(api) == 401
    assert api.posts == [
        (
            COMMENTS_PATH,
            {
                "body": review_policy.codex_request_body(
                    HEAD,
                    workflow_run_id=WORKFLOW_RUN_ID,
                    run_attempt=1,
                    metadata_digest=METADATA_DIGEST,
                    lifecycle_digest=EMPTY_LIFECYCLE_DIGEST,
                )
            },
        )
    ]
    assert api.pull_request_reads == 3


def test_workflow_reuses_its_exact_request_within_one_attempt() -> None:
    comment = _request_comment()
    api = FakeApi(pages={COMMENTS_PATH: [comment]})

    assert _ensure_request(api) == DEFAULT_REQUEST_COMMENT_ID
    assert api.posts == []


def test_workflow_rerun_posts_a_new_request_after_attempt_start() -> None:
    old_comment = _request_comment()
    new_comment = _request_comment(
        comment_id=401,
        created_at="2026-08-15T19:00:03Z",
        updated_at="2026-08-15T19:00:03Z",
        run_attempt=2,
    )
    api = FakeApi(
        pages={COMMENTS_PATH: [old_comment]},
        workflow_run=_workflow_run(run_attempt=2, run_started_at=LATER),
        post_records={COMMENTS_PATH: new_comment},
    )

    assert _ensure_request(api, run_attempt=2) == 401
    assert len(api.posts) == 1


@pytest.mark.parametrize(
    "final_pull_request",
    [
        _pull_request(title=f"{TITLE} while requesting"),
        _pull_request(body=f"{BODY}\n\nWhile requesting."),
    ],
)
def test_request_mode_final_metadata_check_closes_title_and_body_races(
    final_pull_request: dict[str, object],
) -> None:
    api = FakeApi(
        pull_requests=[_pull_request(), final_pull_request],
        pages={COMMENTS_PATH: [_request_comment()]},
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="while requesting review"):
        _ensure_request(api)


def test_created_request_must_be_authored_by_github_actions() -> None:
    forged_comment = _request_comment(
        comment_id=401,
        user_id=review_policy.MAINTAINER_USER_ID,
        login=review_policy.MAINTAINER_LOGIN,
    )
    api = FakeApi(
        pages={COMMENTS_PATH: []},
        post_records={COMMENTS_PATH: forged_comment},
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="unexpected author"):
        _ensure_request(api)


def test_codex_request_cli_emits_only_the_numeric_comment_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    api = FakeApi(pages={COMMENTS_PATH: [_request_comment()]})
    monkeypatch.setattr(
        review_policy,
        "GitHubRestApi",
        lambda *_args, **_kwargs: api,
    )
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    event_path = _write_event_file(tmp_path)

    result = review_policy.main(_codex_request_cli_args(event_path))

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == f"{DEFAULT_REQUEST_COMMENT_ID}\n"
    assert captured.err == ""


def test_large_event_body_is_loaded_from_file_not_argv_or_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    large_body = "x" * review_policy.MAX_PULL_REQUEST_BODY_BYTES
    metadata_digest = review_policy._pull_request_metadata_digest(TITLE, large_body)
    pull_request = _pull_request(body=large_body)
    api = FakeApi(
        pull_requests=[pull_request, pull_request],
        pages={
            COMMENTS_PATH: [
                _request_comment(metadata_digest=metadata_digest),
            ]
        },
    )
    monkeypatch.setattr(
        review_policy,
        "GitHubRestApi",
        lambda *_args, **_kwargs: api,
    )
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.delenv("PR_BODY", raising=False)
    monkeypatch.delenv("PR_TITLE", raising=False)
    event_path = _write_event_file(tmp_path, body=large_body)
    argv = _codex_request_cli_args(event_path)

    assert large_body not in argv
    assert "PR_BODY" not in review_policy.os.environ
    assert "PR_TITLE" not in review_policy.os.environ

    result = review_policy.main(argv)

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == f"{DEFAULT_REQUEST_COMMENT_ID}\n"
    assert captured.err == ""


@pytest.mark.parametrize(
    "case",
    [
        "missing-file",
        "missing-pull-request",
        "missing-title",
        "malformed-json",
        "oversize-event",
        "oversize-body",
    ],
)
def test_codex_cli_rejects_invalid_event_metadata(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "event.json"
    if case == "missing-file":
        pass
    elif case == "missing-pull-request":
        event_path.write_text("{}", encoding="utf-8")
    elif case == "missing-title":
        event_path.write_text(
            json.dumps({"pull_request": {"body": BODY}}),
            encoding="utf-8",
        )
    elif case == "malformed-json":
        event_path.write_text("{", encoding="utf-8")
    elif case == "oversize-event":
        monkeypatch.setattr(review_policy, "MAX_EVENT_PAYLOAD_BYTES", 32)
        event_path.write_bytes(b"x" * 33)
    else:
        _write_event_file(
            tmp_path,
            body="x" * (review_policy.MAX_PULL_REQUEST_BODY_BYTES + 1),
        )
    monkeypatch.setattr(
        review_policy,
        "GitHubRestApi",
        lambda *_args, **_kwargs: FakeApi(),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    result = review_policy.main(_codex_request_cli_args(event_path))

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.startswith("review policy failed:")


def test_codex_cli_normalizes_null_event_body_to_empty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    metadata_digest = review_policy._pull_request_metadata_digest(TITLE, "")
    pull_request = _pull_request(body=None)
    api = FakeApi(
        pull_requests=[pull_request, pull_request],
        pages={
            COMMENTS_PATH: [
                _request_comment(metadata_digest=metadata_digest),
            ]
        },
    )
    monkeypatch.setattr(
        review_policy,
        "GitHubRestApi",
        lambda *_args, **_kwargs: api,
    )
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    event_path = _write_event_file(tmp_path, body=None)

    result = review_policy.main(_codex_request_cli_args(event_path))

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == f"{DEFAULT_REQUEST_COMMENT_ID}\n"
    assert captured.err == ""


REQUEST_REACTIONS_PATH = f"{REQUEST_COMMENT_PATH}/reactions"
EYES_QUERY = f"content=eyes&per_page={review_policy.REACTION_PAGE_SIZE}&page=1"
REQUEST_EYES_PATH = f"{REQUEST_REACTIONS_PATH}?{EYES_QUERY}"
ROOT_EYES_PATH = f"{ROOT_REACTIONS_PATH}?{EYES_QUERY}"


def _review_cycle_api(
    *,
    workflow_run: dict[str, object] | None = None,
    request_comment: dict[str, object] | None = None,
    request_sequence: list[list[dict[str, object]]] | None = None,
    root_sequence: list[list[dict[str, object]]] | None = None,
    pull_requests: list[dict[str, object]] | None = None,
    records: dict[str, object] | None = None,
    record_sequences: dict[str, list[object]] | None = None,
    lifecycle_events: list[dict[str, object]] | None = None,
    lifecycle_sequence: list[list[dict[str, object]]] | None = None,
) -> FakeApi:
    configured_records = dict(records or {})
    if request_comment is not None:
        configured_records[REQUEST_COMMENT_PATH] = request_comment
    configured_sequences = dict(record_sequences or {})
    configured_sequences[REQUEST_EYES_PATH] = request_sequence or [
        [_reaction(content="eyes")],
        [],
    ]
    if root_sequence is not None:
        configured_sequences[ROOT_EYES_PATH] = root_sequence
    page_sequences = None
    if lifecycle_sequence is not None:
        page_sequences = {LIFECYCLE_EVENTS_PATH: lifecycle_sequence}
    return FakeApi(
        pull_requests=pull_requests,
        pages={LIFECYCLE_EVENTS_PATH: lifecycle_events or []},
        workflow_run=workflow_run,
        records=configured_records,
        record_sequences=configured_sequences,
        page_sequences=page_sequences,
    )


def test_formal_review_and_plus_one_reactions_do_not_settle_the_gate() -> None:
    api = FakeApi(
        pages={
            REVIEWS_PATH: [_codex_review()],
        },
        records={
            REQUEST_EYES_PATH: [_reaction(content="+1")],
            ROOT_EYES_PATH: [_reaction(content="+1")],
        },
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api)

    assert ("paginate", REVIEWS_PATH) not in api.calls


@pytest.mark.parametrize(
    ("user_id", "login"),
    [
        (
            review_policy.CODEX_CONNECTOR_USER_ID,
            "chatgpt-codex-connector-lookalike",
        ),
        (42, review_policy.CODEX_CONNECTOR_LOGIN),
    ],
)
def test_review_start_requires_both_connector_id_and_login(
    user_id: int,
    login: str,
) -> None:
    api = FakeApi(
        records={REQUEST_EYES_PATH: [_reaction(content="eyes", user_id=user_id, login=login)]}
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api)


@pytest.mark.parametrize(
    ("page_size", "message"),
    [
        (review_policy.REACTION_PAGE_SIZE, "ambiguous at its page limit"),
        (review_policy.REACTION_PAGE_SIZE + 1, "exceeded its page limit"),
    ],
)
def test_connector_eyes_query_fails_closed_at_its_page_limit(
    page_size: int,
    message: str,
) -> None:
    reactions = [
        _reaction(
            reaction_id=index + 1,
            user_id=42,
            login="contributor",
            content="eyes",
        )
        for index in range(page_size)
    ]
    api = FakeApi(records={REQUEST_EYES_PATH: reactions})

    with pytest.raises(review_policy.ReviewPolicyError, match=message):
        _codex_check(api)


def test_exact_request_connector_eyes_must_be_observed_before_settlement() -> None:
    api = FakeApi()

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api)


def test_request_eyes_may_share_the_request_timestamp_second() -> None:
    api = _review_cycle_api()

    assert _codex_poll_check(api) == "Codex connector review request settled"


def test_connector_eyes_before_the_request_do_not_start_this_review() -> None:
    api = _review_cycle_api(
        request_sequence=[
            [_reaction(created_at=BASELINE, content="eyes")],
            [],
        ]
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_poll_check(api)


def test_current_connector_eyes_on_the_request_blocks_settlement() -> None:
    api = FakeApi(records={REQUEST_EYES_PATH: [_reaction(content="eyes")]})

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api)


def test_current_connector_eyes_on_the_pull_request_root_blocks_settlement() -> None:
    api = _review_cycle_api(
        root_sequence=[[_reaction(content="eyes")]],
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api)


def test_root_connector_eyes_do_not_count_as_request_review_start() -> None:
    api = FakeApi(
        record_sequences={
            ROOT_EYES_PATH: [[_reaction(content="eyes")], []],
        }
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_poll_check(api)


def test_non_connector_eyes_are_ignored_after_connector_start() -> None:
    api = _review_cycle_api(
        request_sequence=[
            [_reaction(content="eyes")],
            [
                _reaction(
                    content="eyes",
                    user_id=42,
                    login="contributor",
                )
            ],
            [],
        ]
    )

    assert _codex_poll_check(api) == "Codex connector review request settled"


def test_one_eye_free_poll_is_not_enough_to_settle() -> None:
    api = _review_cycle_api()

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_check(api)


def test_two_consecutive_eye_free_polls_and_confirmation_settle() -> None:
    api = _review_cycle_api()

    assert _codex_poll_check(api) == "Codex connector review request settled"
    assert api.calls.count(("get", REQUEST_EYES_PATH)) == 4
    assert api.calls.count(("get", ROOT_EYES_PATH)) == 3
    assert api.calls.count(("get", REQUEST_COMMENT_PATH)) == 2
    assert api.pull_request_reads == 3
    assert api.calls.count(("paginate", LIFECYCLE_EVENTS_PATH)) == 3
    assert ("paginate", REQUEST_REACTIONS_PATH) not in api.calls
    assert ("paginate", ROOT_REACTIONS_PATH) not in api.calls


def test_reappearing_request_eyes_reset_the_eye_free_grace() -> None:
    api = _review_cycle_api(
        request_sequence=[
            [_reaction(reaction_id=201, content="eyes")],
            [],
            [_reaction(reaction_id=202, created_at=LATER, content="eyes")],
            [],
        ]
    )
    clock = iter([0.0, 0.0, 0.0, 0.0])

    result = review_policy.check_codex_policy(
        api,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        expected_head=HEAD,
        expected_base_ref=BASE_REF,
        expected_base_sha=BASE_SHA,
        action="opened",
        baseline_time=BASELINE,
        workflow_run_id=WORKFLOW_RUN_ID,
        run_attempt=1,
        expected_title=TITLE,
        expected_body=BODY,
        request_comment_id=DEFAULT_REQUEST_COMMENT_ID,
        poll_timeout=1,
        poll_interval=1,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )

    assert result == "Codex connector review request settled"
    assert api.calls.count(("get", REQUEST_EYES_PATH)) == 6


@pytest.mark.parametrize("reaction_location", ["request", "root"])
def test_connector_eyes_reappearing_during_confirmation_fail_closed(
    reaction_location: str,
) -> None:
    request_sequence = [[_reaction(content="eyes")], [], []]
    root_sequence: list[list[dict[str, object]]] | None = None
    if reaction_location == "request":
        request_sequence.append([_reaction(created_at=LATER, content="eyes")])
    else:
        root_sequence = [[], [], [_reaction(created_at=LATER, content="eyes")]]
    api = _review_cycle_api(
        request_sequence=request_sequence,
        root_sequence=root_sequence,
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="timed out"):
        _codex_poll_check(api)


@pytest.mark.parametrize(
    ("action", "lifecycle_event"),
    [
        ("opened", None),
        ("synchronize", None),
        ("reopened", "reopened"),
        ("edited", None),
        ("ready_for_review", "ready_for_review"),
    ],
)
def test_every_eligible_event_uses_the_same_request_lifecycle(
    action: str,
    lifecycle_event: str | None,
) -> None:
    lifecycle_events = (
        []
        if lifecycle_event is None
        else [_issue_event(event=lifecycle_event, created_at=BASELINE)]
    )
    api = _review_cycle_api(
        request_comment=_request_comment(
            lifecycle_digest=_expected_lifecycle_digest(lifecycle_events)
        ),
        lifecycle_events=lifecycle_events,
    )

    assert _codex_poll_check(api, action=action) == ("Codex connector review request settled")


def test_stable_lifecycle_history_remains_bound_through_final_confirmation() -> None:
    lifecycle_events = [
        _issue_event(event_id=501, event="closed", created_at="2026-08-15T17:00:00Z"),
        _issue_event(event_id=502, event="reopened", created_at=PRIOR_BASELINE),
    ]
    api = _review_cycle_api(
        request_comment=_request_comment(
            lifecycle_digest=_expected_lifecycle_digest(lifecycle_events)
        ),
        lifecycle_events=lifecycle_events,
    )

    assert _codex_poll_check(api) == "Codex connector review request settled"
    assert api.calls.count(("paginate", LIFECYCLE_EVENTS_PATH)) == 3


@pytest.mark.parametrize(
    "lifecycle_events",
    [
        [
            _issue_event(event_id=501, event="closed", created_at=FRESH),
            _issue_event(event_id=502, event="reopened", created_at=LATER),
        ],
        [
            _issue_event(event_id=501, event="convert_to_draft", created_at=FRESH),
            _issue_event(event_id=502, event="ready_for_review", created_at=LATER),
        ],
    ],
    ids=("close-reopen", "draft-ready"),
)
def test_pre_request_lifecycle_round_trip_is_rejected(
    lifecycle_events: list[dict[str, object]],
) -> None:
    api = FakeApi(pages={LIFECYCLE_EVENTS_PATH: lifecycle_events})

    with pytest.raises(review_policy.ReviewPolicyError, match="changed after the triggering event"):
        _ensure_request(api)

    assert ("get", CURRENT_WORKFLOW_RUN_PATH) not in api.calls


def test_pre_request_same_second_lifecycle_round_trip_is_ambiguous() -> None:
    lifecycle_events = [
        _issue_event(event_id=501, event="closed", created_at=BASELINE),
        _issue_event(event_id=502, event="reopened", created_at=BASELINE),
    ]
    api = FakeApi(pages={LIFECYCLE_EVENTS_PATH: lifecycle_events})

    with pytest.raises(review_policy.ReviewPolicyError, match="ambiguous at the event baseline"):
        _ensure_request(api)


@pytest.mark.parametrize(
    "lifecycle_events",
    [
        [
            _issue_event(event_id=501, event="closed", created_at=FRESH),
            _issue_event(event_id=502, event="reopened", created_at=LATER),
        ],
        [
            _issue_event(event_id=501, event="convert_to_draft", created_at=FRESH),
            _issue_event(event_id=502, event="ready_for_review", created_at=LATER),
        ],
    ],
    ids=("close-reopen", "draft-ready"),
)
def test_post_lineage_lifecycle_round_trip_is_rejected(
    lifecycle_events: list[dict[str, object]],
) -> None:
    api = _review_cycle_api(lifecycle_sequence=[[], lifecycle_events])

    with pytest.raises(review_policy.ReviewPolicyError, match="changed after the triggering event"):
        _codex_poll_check(api)

    assert api.calls.count(("paginate", LIFECYCLE_EVENTS_PATH)) == 2


def test_post_lineage_same_second_lifecycle_round_trip_is_ambiguous() -> None:
    lifecycle_events = [
        _issue_event(event_id=501, event="closed", created_at=BASELINE),
        _issue_event(event_id=502, event="reopened", created_at=BASELINE),
    ]
    api = _review_cycle_api(lifecycle_sequence=[[], lifecycle_events])

    with pytest.raises(review_policy.ReviewPolicyError, match="ambiguous at the event baseline"):
        _codex_poll_check(api)


def test_lifecycle_change_between_final_bracket_queries_fails_closed() -> None:
    lifecycle_events = [
        _issue_event(event_id=501, event="closed", created_at=FRESH),
        _issue_event(event_id=502, event="reopened", created_at=LATER),
    ]
    api = _review_cycle_api(lifecycle_sequence=[[], [], lifecycle_events])

    with pytest.raises(review_policy.ReviewPolicyError, match="changed after the triggering event"):
        _codex_poll_check(api)

    assert api.calls.count(("paginate", LIFECYCLE_EVENTS_PATH)) == 3


@pytest.mark.parametrize(
    ("action", "event"),
    [("reopened", "reopened"), ("ready_for_review", "ready_for_review")],
)
def test_lifecycle_trigger_requires_one_unique_same_second_event(
    action: str,
    event: str,
) -> None:
    lifecycle_events = [
        _issue_event(event_id=501, event=event, created_at=BASELINE),
        _issue_event(event_id=502, event=event, created_at=BASELINE),
    ]
    api = FakeApi(pages={LIFECYCLE_EVENTS_PATH: lifecycle_events})

    with pytest.raises(
        review_policy.ReviewPolicyError, match="does not match the triggering event"
    ):
        _ensure_request(api, action=action)


def test_request_marker_lifecycle_digest_must_match_initial_events() -> None:
    mismatched_digest = "f" * 64
    assert mismatched_digest != EMPTY_LIFECYCLE_DIGEST
    api = _review_cycle_api(request_comment=_request_comment(lifecycle_digest=mismatched_digest))

    with pytest.raises(review_policy.ReviewPolicyError, match="expected head"):
        _codex_check(api)


@pytest.mark.parametrize(
    ("lifecycle_events", "message"),
    [
        ([_issue_event(event=None)], "issue event type is invalid"),
        ([_issue_event(event="x" * 65)], "issue event type is invalid"),
        ([_issue_event(event_id=0)], "bounded positive integer"),
        (
            [
                _issue_event(event_id=501),
                _issue_event(event_id=501, event="reopened"),
            ],
            "event ID is repeated",
        ),
        ([_issue_event(created_at="not-a-timestamp")], "ISO-8601 timestamp"),
    ],
    ids=(
        "missing-event",
        "oversize-event",
        "invalid-id",
        "duplicate-id",
        "invalid-timestamp",
    ),
)
def test_lifecycle_event_records_fail_closed_when_malformed(
    lifecycle_events: list[dict[str, object]],
    message: str,
) -> None:
    api = FakeApi(pages={LIFECYCLE_EVENTS_PATH: lifecycle_events})

    with pytest.raises(review_policy.ReviewPolicyError, match=message):
        _lifecycle_digest(api)


def test_rerun_requires_its_own_attempt_bound_request() -> None:
    workflow_run = _workflow_run(run_attempt=2)
    stale_request = _request_comment(run_attempt=1)
    api = FakeApi(
        workflow_run=workflow_run,
        records={REQUEST_COMMENT_PATH: stale_request},
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="expected head"):
        _codex_check(api, run_attempt=2)


def test_rerun_settles_only_after_eyes_on_its_exact_request() -> None:
    workflow_run = _workflow_run(run_attempt=2)
    api = _review_cycle_api(workflow_run=workflow_run)

    assert _codex_poll_check(api, run_attempt=2) == ("Codex connector review request settled")


def test_superseded_workflow_rerun_cannot_settle() -> None:
    old_run_id = 31_915_167_864
    old_run = _workflow_run(
        run_id=old_run_id,
        run_number=WORKFLOW_RUN_NUMBER - 1,
        run_attempt=2,
    )
    newer_run = _workflow_run(created_at=FRESH)
    api = _review_cycle_api(
        workflow_run=old_run,
        records={
            f"/repos/{REPOSITORY}/actions/runs/{old_run_id}": old_run,
            WORKFLOW_RUNS_PATH: {
                "total_count": 2,
                "workflow_runs": [newer_run, old_run],
            },
        },
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="superseded"):
        _codex_poll_check(
            api,
            workflow_run_id=old_run_id,
            run_attempt=2,
        )


def test_workflow_history_index_lag_is_retried_before_success() -> None:
    current_run = _workflow_run()
    api = _review_cycle_api(
        record_sequences={
            WORKFLOW_RUNS_PATH: [
                {"total_count": 0, "workflow_runs": []},
                {"total_count": 1, "workflow_runs": [current_run]},
            ]
        }
    )

    result = review_policy.check_codex_policy(
        api,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        expected_head=HEAD,
        expected_base_ref=BASE_REF,
        expected_base_sha=BASE_SHA,
        action="opened",
        baseline_time=BASELINE,
        workflow_run_id=WORKFLOW_RUN_ID,
        run_attempt=1,
        expected_title=TITLE,
        expected_body=BODY,
        request_comment_id=DEFAULT_REQUEST_COMMENT_ID,
        poll_timeout=1,
        poll_interval=1,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )

    assert result == "Codex connector review request settled"
    assert api.record_reads[WORKFLOW_RUNS_PATH] == 2


def test_workflow_history_ignores_a_newer_run_for_another_pull_request() -> None:
    current_run = _workflow_run()
    other_pull_request_run = _workflow_run(
        run_id=WORKFLOW_RUN_ID + 1,
        run_number=WORKFLOW_RUN_NUMBER + 1,
        pull_request_number=PULL_REQUEST + 1,
        created_at=FRESH,
    )
    api = _review_cycle_api(
        records={
            WORKFLOW_RUNS_PATH: {
                "total_count": 2,
                "workflow_runs": [other_pull_request_run, current_run],
            }
        }
    )

    assert _codex_poll_check(api) == "Codex connector review request settled"


@pytest.mark.parametrize(
    "workflow_run",
    [
        _workflow_run(event="pull_request"),
        _workflow_run(path=".github/workflows/lookalike.yml"),
        _workflow_run(base_sha=OTHER_BASE_SHA),
    ],
)
def test_current_workflow_run_identity_is_fail_closed(
    workflow_run: dict[str, object],
) -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="workflow run"):
        _codex_check(FakeApi(workflow_run=workflow_run))


def test_current_workflow_run_requires_a_started_at_timestamp() -> None:
    workflow_run = _workflow_run()
    workflow_run.pop("run_started_at")

    with pytest.raises(review_policy.ReviewPolicyError, match="workflow run start"):
        _codex_check(FakeApi(workflow_run=workflow_run))


def test_current_workflow_run_cannot_start_before_creation() -> None:
    workflow_run = _workflow_run(created_at=FRESH, run_started_at=BASELINE)

    with pytest.raises(review_policy.ReviewPolicyError, match="started before"):
        _codex_check(FakeApi(workflow_run=workflow_run))


def test_workflow_history_must_preserve_current_attempt_start() -> None:
    current_run = _workflow_run(run_attempt=2, run_started_at=FRESH)
    mismatched_history = _workflow_run(run_attempt=2, run_started_at=LATER)
    api = _review_cycle_api(
        workflow_run=current_run,
        records={
            WORKFLOW_RUNS_PATH: {
                "total_count": 1,
                "workflow_runs": [mismatched_history],
            }
        },
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="identity changed"):
        _codex_poll_check(api, run_attempt=2)


def test_request_comment_id_is_required() -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="workflow-authored"):
        _codex_check(FakeApi(), request_comment_id=None)


def test_request_comment_must_be_authored_by_github_actions() -> None:
    forged_comment = _request_comment(
        user_id=review_policy.MAINTAINER_USER_ID,
        login=review_policy.MAINTAINER_LOGIN,
    )
    api = FakeApi(records={REQUEST_COMMENT_PATH: forged_comment})

    with pytest.raises(review_policy.ReviewPolicyError, match="trusted workflow"):
        _codex_check(api)


@pytest.mark.parametrize(
    "comment",
    [
        _request_comment(head=OTHER_HEAD),
        _request_comment(run_attempt=2),
    ],
)
def test_request_comment_must_match_the_exact_attempt_body(
    comment: dict[str, object],
) -> None:
    api = FakeApi(records={REQUEST_COMMENT_PATH: comment})

    with pytest.raises(review_policy.ReviewPolicyError, match="expected head"):
        _codex_check(api)


@pytest.mark.parametrize(
    "initial_pull_request",
    [
        _pull_request(title=f"{TITLE} after edit"),
        _pull_request(body=f"{BODY}\n\nAfter edit."),
    ],
)
def test_request_metadata_digest_must_match_initial_title_and_body(
    initial_pull_request: dict[str, object],
) -> None:
    api = FakeApi(
        pull_requests=[initial_pull_request],
        records={REQUEST_COMMENT_PATH: _request_comment()},
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="does not match the event"):
        _codex_check(api)


def test_request_comment_must_belong_to_the_pull_request() -> None:
    comment = _request_comment()
    comment["issue_url"] = f"https://api.github.com/repos/{REPOSITORY}/issues/{PULL_REQUEST + 1}"
    api = FakeApi(records={REQUEST_COMMENT_PATH: comment})

    with pytest.raises(review_policy.ReviewPolicyError, match="does not belong"):
        _codex_check(api)


def test_request_comment_must_not_predate_event_baseline() -> None:
    comment = _request_comment(
        created_at=PRIOR_BASELINE,
        updated_at=FRESH,
    )
    api = FakeApi(records={REQUEST_COMMENT_PATH: comment})

    with pytest.raises(review_policy.ReviewPolicyError, match="predates"):
        _codex_check(api)


def test_explicit_request_id_does_not_scan_unrelated_comments() -> None:
    attacker_comment = _request_comment(
        comment_id=300,
        user_id=42,
        login="contributor",
    )
    api = _review_cycle_api()
    api.pages[COMMENTS_PATH] = [attacker_comment]

    assert _codex_poll_check(api) == "Codex connector review request settled"
    assert ("paginate", COMMENTS_PATH) not in api.calls


def test_request_comment_change_during_confirmation_fails_closed() -> None:
    original = _request_comment()
    changed = _request_comment(updated_at=LATER)
    api = _review_cycle_api(
        record_sequences={
            REQUEST_COMMENT_PATH: [original, changed],
        }
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="changed during review"):
        _codex_poll_check(api)


def test_final_live_head_check_closes_settlement_race() -> None:
    api = _review_cycle_api(
        pull_requests=[
            _pull_request(),
            _pull_request(head=OTHER_HEAD),
        ]
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="live pull request head"):
        _codex_poll_check(api)


@pytest.mark.parametrize(
    ("final_pull_request", "message"),
    [
        (_pull_request(base_ref="release"), "base ref"),
        (_pull_request(base_sha=OTHER_BASE_SHA), "base SHA"),
    ],
)
def test_final_live_base_check_closes_settlement_race(
    final_pull_request: dict[str, object],
    message: str,
) -> None:
    api = _review_cycle_api(
        pull_requests=[
            _pull_request(),
            final_pull_request,
        ]
    )

    with pytest.raises(review_policy.ReviewPolicyError, match=message):
        _codex_poll_check(api)


@pytest.mark.parametrize(
    "final_pull_request",
    [
        _pull_request(title=f"{TITLE} after review"),
        _pull_request(body=f"{BODY}\n\nAfter review."),
    ],
)
def test_final_metadata_digest_closes_title_and_body_edit_races(
    final_pull_request: dict[str, object],
) -> None:
    api = _review_cycle_api(
        pull_requests=[
            _pull_request(),
            final_pull_request,
        ]
    )

    with pytest.raises(review_policy.ReviewPolicyError, match="title or body changed"):
        _codex_poll_check(api)


def test_reaction_driven_pull_request_update_with_same_metadata_still_settles() -> None:
    api = _review_cycle_api(
        pull_requests=[
            _pull_request(updated_at=BASELINE),
            _pull_request(updated_at=LATER),
            _pull_request(updated_at=LATER),
        ]
    )

    assert _codex_poll_check(api) == "Codex connector review request settled"
    assert api.pull_request_reads == 3


def test_pull_request_activity_change_across_final_bracket_fails_closed() -> None:
    api = _review_cycle_api(
        pull_requests=[
            _pull_request(updated_at=BASELINE),
            _pull_request(updated_at=LATER),
            _pull_request(updated_at=LATEST),
        ]
    )

    with pytest.raises(
        review_policy.ReviewPolicyError,
        match="changed during Codex review final confirmation",
    ):
        _codex_poll_check(api)


def test_request_mode_activity_change_across_final_bracket_fails_closed() -> None:
    api = FakeApi(
        pull_requests=[
            _pull_request(updated_at=BASELINE),
            _pull_request(updated_at=LATER),
            _pull_request(updated_at=LATEST),
        ],
        pages={COMMENTS_PATH: [_request_comment()]},
    )

    with pytest.raises(
        review_policy.ReviewPolicyError,
        match="changed while requesting review final confirmation",
    ):
        _ensure_request(api)


def test_rest_null_body_matches_an_empty_event_body() -> None:
    empty_body_digest = review_policy._pull_request_metadata_digest(TITLE, "")
    null_body_pull_request = _pull_request(body=None)
    api = _review_cycle_api(
        request_comment=_request_comment(metadata_digest=empty_body_digest),
        pull_requests=[null_body_pull_request, null_body_pull_request],
    )

    assert _codex_poll_check(api, expected_body="") == ("Codex connector review request settled")


def test_crlf_rest_body_matches_lf_event_body() -> None:
    event_body = f"{BODY}\nSecond paragraph."
    rest_body = event_body.replace("\n", "\r\n")
    api = _review_cycle_api(
        request_comment=_request_comment(
            metadata_digest=review_policy._pull_request_metadata_digest(TITLE, event_body)
        ),
        pull_requests=[
            _pull_request(body=rest_body),
            _pull_request(body=rest_body),
        ],
    )

    assert _codex_poll_check(api, expected_body=event_body) == (
        "Codex connector review request settled"
    )


@pytest.mark.parametrize(
    ("action", "message"),
    [
        ("closed", "closed"),
        ("converted_to_draft", "converted-to-draft"),
    ],
)
def test_terminal_action_fails_immediately_even_if_api_has_not_caught_up(
    action: str,
    message: str,
) -> None:
    api = FakeApi()

    with pytest.raises(review_policy.ReviewPolicyError, match=message):
        _codex_check(api, action=action)

    assert api.pull_request_reads == 1
    assert ("get", CURRENT_WORKFLOW_RUN_PATH) not in api.calls
    assert ("get", REQUEST_COMMENT_PATH) not in api.calls


@pytest.mark.parametrize(
    ("timeout", "interval"),
    [(-1, 1), (1_801, 1), (0, 0), (0, 61)],
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
            workflow_run_id=WORKFLOW_RUN_ID,
            run_attempt=1,
            expected_title=TITLE,
            expected_body=BODY,
            request_comment_id=DEFAULT_REQUEST_COMMENT_ID,
            poll_timeout=timeout,
            poll_interval=interval,
        )


def test_api_budget_covers_the_workflow_poll_configuration() -> None:
    configured_interval = 15
    maximum_polls = int(review_policy.MAX_POLL_TIMEOUT_SECONDS / configured_interval) + 1
    reaction_requests_per_poll = 2
    lifecycle_snapshots = 3
    lifecycle_request_allowance = lifecycle_snapshots * review_policy.MAX_API_PAGES
    setup_and_confirmation_allowance = 12

    assert review_policy.MAX_API_REQUESTS >= (
        maximum_polls * reaction_requests_per_poll
        + lifecycle_request_allowance
        + setup_and_confirmation_allowance
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


def test_rest_client_posts_bounded_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Any] = []

    def open_request(
        request: Any,
        **_kwargs: object,
    ) -> FakeResponse:
        observed.append(request)
        return FakeResponse()

    monkeypatch.setattr(review_policy.urllib.request, "urlopen", open_request)
    api = review_policy.GitHubRestApi("test-token")

    assert api.post("/issues/1/comments", {"body": "@codex review"}) == {}
    assert len(observed) == 1
    request = observed[0]
    assert request.method == "POST"
    assert request.data == b'{"body":"@codex review"}'
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


def test_lifecycle_event_collection_inherits_the_rest_page_limit() -> None:
    full_pages = [
        [{"event": "assigned"} for _index in range(100)]
        for _page in range(review_policy.MAX_API_PAGES)
    ]
    api = PagingApi(full_pages)

    with pytest.raises(review_policy.ReviewPolicyError, match="page limit"):
        _lifecycle_digest(api)

    assert api.paths[0] == f"{LIFECYCLE_EVENTS_PATH}?per_page=100&page=1"
    assert len(api.paths) == review_policy.MAX_API_PAGES
