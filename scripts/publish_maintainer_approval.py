"""Publish current-head maintainer approval statuses from a trusted workflow.

The unprivileged event workflow can be triggered by pull-request review events
without receiving a write token.  A ``workflow_run`` subscriber executes this
module from the default branch, resolves exact-head pull requests from live
GitHub data, and replaces one commit-status context for each affected head.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass

from scripts import check_pr_review_policy as review_policy

EVENT_WORKFLOW_PATH = ".github/workflows/maintainer-approval-event.yml"
EVENT_WORKFLOW_EVENTS = frozenset({"pull_request_review", "pull_request_target"})
TARGET_EVENT_ACTIONS = frozenset(
    {
        "closed",
        "converted_to_draft",
        "edited",
        "opened",
        "ready_for_review",
        "reopened",
        "synchronize",
    }
)
REVIEW_SOURCE_ACTION = "review"
EXPECTED_SOURCE_ACTIONS = TARGET_EVENT_ACTIONS | {REVIEW_SOURCE_ACTION}
STATUS_CONTEXT = "Maintainer Approval"
MAX_STATUS_DESCRIPTION_CHARS = 140
MAX_STATUSES_PER_CONTEXT = 1_000
SOURCE_DISPLAY_TITLE = re.compile(
    r'\A\{"pr":(?P<number>[1-9][0-9]*), '
    r'"head":"(?P<head>[0-9a-f]{40})", '
    r'"previous":"(?P<previous_head>[0-9a-f]{40})", '
    r'"action":"(?P<action>[a-z_]+)"\}\Z'
)


@dataclass(frozen=True)
class SourceWorkflowRun:
    """Security-relevant fields from the unprivileged event workflow run."""

    run_id: int
    pull_request_head: str
    previous_pull_request_head: str
    action: str
    event: str
    conclusion: str


@dataclass(frozen=True)
class HeadOwnership:
    """Live exact-head owner and evidence that GitHub knows the commit's PRs."""

    pull_request: review_policy.PullRequest | None
    association_count: int


def _source_workflow_run(
    api: review_policy.RestApi,
    *,
    repository: str,
    run_id: int,
    expected_head: str,
    expected_previous_head: str,
    expected_source_action: str,
) -> SourceWorkflowRun:
    payload = review_policy._record(
        api.get(f"/repos/{repository}/actions/runs/{run_id}"),
        "maintainer approval event workflow run",
    )
    payload_id = review_policy._positive_int(
        payload.get("id"), "maintainer approval event workflow run ID"
    )
    if payload_id != run_id:
        raise review_policy.ReviewPolicyError(
            "GitHub returned a different maintainer approval event workflow run"
        )
    if payload.get("path") != EVENT_WORKFLOW_PATH:
        raise review_policy.ReviewPolicyError(
            "maintainer approval event workflow run has an unexpected path"
        )
    event = payload.get("event")
    if event not in EVENT_WORKFLOW_EVENTS:
        raise review_policy.ReviewPolicyError(
            "maintainer approval event workflow run has an unexpected event"
        )
    if payload.get("status") != "completed":
        raise review_policy.ReviewPolicyError(
            "maintainer approval event workflow run is not complete"
        )
    conclusion = payload.get("conclusion")
    if not isinstance(conclusion, str) or not conclusion or len(conclusion) > 32:
        raise review_policy.ReviewPolicyError(
            "maintainer approval event workflow run conclusion is invalid"
        )
    if event == "pull_request_review":
        if expected_source_action != REVIEW_SOURCE_ACTION:
            raise review_policy.ReviewPolicyError(
                "maintainer approval review workflow run has an unexpected action marker"
            )
        pull_request_head = review_policy._commit_sha(
            payload.get("head_sha"),
            "maintainer approval review workflow run head SHA",
        )
        previous_pull_request_head = pull_request_head
        if pull_request_head != expected_head or expected_previous_head != pull_request_head:
            raise review_policy.ReviewPolicyError(
                "maintainer approval review workflow run has an unexpected head"
            )
        action = REVIEW_SOURCE_ACTION
    else:
        display_title = payload.get("display_title")
        if not isinstance(display_title, str):
            raise review_policy.ReviewPolicyError(
                "maintainer approval event workflow run title is invalid"
            )
        title_match = SOURCE_DISPLAY_TITLE.fullmatch(display_title)
        if title_match is None:
            raise review_policy.ReviewPolicyError(
                "maintainer approval event workflow run title is invalid"
            )
        review_policy._positive_int(
            int(title_match.group("number")),
            "maintainer approval event pull request number",
        )
        pull_request_head = review_policy._commit_sha(
            title_match.group("head"),
            "maintainer approval event pull request head",
        )
        if pull_request_head != expected_head:
            raise review_policy.ReviewPolicyError(
                "maintainer approval event workflow run has an unexpected head"
            )
        previous_pull_request_head = review_policy._commit_sha(
            title_match.group("previous_head"),
            "maintainer approval event previous pull request head",
        )
        if previous_pull_request_head != expected_previous_head:
            raise review_policy.ReviewPolicyError(
                "maintainer approval event workflow run has an unexpected previous head"
            )
        action = title_match.group("action")
        if action not in TARGET_EVENT_ACTIONS or action != expected_source_action:
            raise review_policy.ReviewPolicyError(
                "maintainer approval event workflow run has an unexpected action"
            )
    return SourceWorkflowRun(
        run_id=payload_id,
        pull_request_head=pull_request_head,
        previous_pull_request_head=previous_pull_request_head,
        action=action,
        event=event,
        conclusion=conclusion,
    )


def _head_ownership(
    api: review_policy.RestApi,
    *,
    repository: str,
    head_sha: str,
) -> HeadOwnership:
    records = api.paginate(f"/repos/{repository}/commits/{head_sha}/pulls")
    open_pull_request_numbers: list[int] = []
    for index, item in enumerate(records):
        record = review_policy._record(
            item, f"pull request associated with maintainer approval head {index}"
        )
        state = record.get("state")
        if state not in {"open", "closed"}:
            raise review_policy.ReviewPolicyError(
                "pull request associated with maintainer approval head has an invalid state"
            )
        pull_request_number = review_policy._positive_int(
            record.get("number"), "associated pull request number"
        )
        head = review_policy._record(record.get("head"), "associated pull request head")
        associated_head_sha = review_policy._commit_sha(
            head.get("sha"), "associated pull request head SHA"
        )
        if state != "open":
            continue
        if associated_head_sha != head_sha:
            continue
        open_pull_request_numbers.append(pull_request_number)
    if len(open_pull_request_numbers) > 1:
        raise review_policy.ReviewPolicyError(
            "maintainer approval head does not identify exactly one open pull request"
        )
    if not open_pull_request_numbers:
        return HeadOwnership(pull_request=None, association_count=len(records))
    pull_request = review_policy._load_pull_request(api, repository, open_pull_request_numbers[0])
    if pull_request.state != "open" or pull_request.head_sha != head_sha:
        raise review_policy.ReviewPolicyError(
            "associated pull request no longer owns the maintainer approval head"
        )
    return HeadOwnership(pull_request=pull_request, association_count=len(records))


def _same_candidate(
    before: review_policy.PullRequest,
    after: review_policy.PullRequest | None,
) -> bool:
    return after is not None and (
        after.number,
        after.head_sha,
        after.base_ref,
        after.base_sha,
        after.draft,
        after.state,
    ) == (
        before.number,
        before.head_sha,
        before.base_ref,
        before.base_sha,
        before.draft,
        before.state,
    )


def _policy_state(
    api: review_policy.RestApi,
    *,
    repository: str,
    pull_request: review_policy.PullRequest,
) -> tuple[str, str]:
    try:
        result = review_policy.check_maintainer_policy(
            api,
            repository=repository,
            pull_request_number=pull_request.number,
            expected_head=pull_request.head_sha,
            expected_base_ref=pull_request.base_ref,
            expected_base_sha=pull_request.base_sha,
        )
    except review_policy.ReviewPolicyError as error:
        return "failure", str(error)
    return "success", result


def _status_context_usage(
    api: review_policy.RestApi,
    *,
    repository: str,
    head_sha: str,
    target_url: str,
) -> tuple[int, bool]:
    records = api.paginate(f"/repos/{repository}/commits/{head_sha}/statuses")
    status_ids: set[int] = set()
    pending_for_run = False
    for index, item in enumerate(records):
        record = review_policy._record(item, f"maintainer approval status {index}")
        if record.get("context") != STATUS_CONTEXT:
            continue
        status_id = review_policy._positive_int(record.get("id"), "maintainer approval status ID")
        if status_id in status_ids:
            raise review_policy.ReviewPolicyError("maintainer approval status ID is repeated")
        status_ids.add(status_id)
        state = record.get("state")
        if state not in {"error", "failure", "pending", "success"}:
            raise review_policy.ReviewPolicyError("maintainer approval status state is invalid")
        pending_for_run |= state == "pending" and record.get("target_url") == target_url
    return len(status_ids), pending_for_run


def _capacity_adjusted_state(
    api: review_policy.RestApi,
    *,
    repository: str,
    head_sha: str,
    target_url: str,
    state: str,
    result: str,
) -> tuple[str, str] | None:
    status_count, pending_for_run = _status_context_usage(
        api,
        repository=repository,
        head_sha=head_sha,
        target_url=target_url,
    )
    # Every publisher job posts this attempt's pending state before any
    # fallible read. Account for read-after-write lag conservatively.
    if not pending_for_run:
        status_count += 1
    if status_count >= MAX_STATUSES_PER_CONTEXT:
        return None
    if status_count == MAX_STATUSES_PER_CONTEXT - 1:
        return (
            "failure",
            "maintainer approval status capacity is exhausted; push a new head",
        )
    return state, result


def _status_description(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        normalized = "Maintainer approval state is unavailable"
    return normalized[:MAX_STATUS_DESCRIPTION_CHARS]


def _publish_status(
    api: review_policy.RestApi,
    *,
    repository: str,
    head_sha: str,
    state: str,
    description: str,
    target_url: str,
) -> None:
    payload = review_policy._record(
        api.post(
            f"/repos/{repository}/statuses/{head_sha}",
            {
                "state": state,
                "context": STATUS_CONTEXT,
                "description": _status_description(description),
                "target_url": target_url,
            },
        ),
        "maintainer approval status",
    )
    review_policy._positive_int(payload.get("id"), "maintainer approval status ID")
    if payload.get("state") != state or payload.get("context") != STATUS_CONTEXT:
        raise review_policy.ReviewPolicyError(
            "GitHub returned a different maintainer approval status"
        )


def _details_url(
    server_url: str,
    repository: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
) -> str:
    parsed = urllib.parse.urlsplit(server_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise review_policy.ReviewPolicyError("GitHub server URL must be an HTTPS origin")
    return (
        f"{parsed.scheme}://{parsed.netloc}/{repository}/actions/runs/"
        f"{workflow_run_id}/attempts/{workflow_run_attempt}"
    )


def publish_maintainer_approval(
    api: review_policy.RestApi,
    *,
    repository: str,
    source_workflow_run_id: int,
    publisher_workflow_run_id: int,
    publisher_workflow_run_attempt: int,
    server_url: str,
    expected_head: str,
    expected_previous_head: str,
    expected_source_action: str,
    candidate_head: str,
) -> str:
    """Replace approval status for one affected current or displaced head."""
    repository = review_policy._repository(repository)
    source_workflow_run_id = review_policy._positive_int(
        source_workflow_run_id, "source workflow run ID"
    )
    publisher_workflow_run_id = review_policy._positive_int(
        publisher_workflow_run_id, "publisher workflow run ID"
    )
    publisher_workflow_run_attempt = review_policy._positive_int(
        publisher_workflow_run_attempt, "publisher workflow run attempt"
    )
    expected_head = review_policy._commit_sha(expected_head, "expected pull request head")
    expected_previous_head = review_policy._commit_sha(
        expected_previous_head, "expected previous pull request head"
    )
    if expected_source_action not in EXPECTED_SOURCE_ACTIONS:
        raise review_policy.ReviewPolicyError("expected source action is invalid")
    candidate_head = review_policy._commit_sha(candidate_head, "candidate head")
    target_url = _details_url(
        server_url,
        repository,
        publisher_workflow_run_id,
        publisher_workflow_run_attempt,
    )
    source = _source_workflow_run(
        api,
        repository=repository,
        run_id=source_workflow_run_id,
        expected_head=expected_head,
        expected_previous_head=expected_previous_head,
        expected_source_action=expected_source_action,
    )
    candidate_heads = {source.pull_request_head}
    if (
        source.action == "synchronize"
        and source.previous_pull_request_head != source.pull_request_head
    ):
        candidate_heads.add(source.previous_pull_request_head)
    if candidate_head not in candidate_heads:
        raise review_policy.ReviewPolicyError(
            "candidate head is not affected by the maintainer approval event"
        )
    try:
        ownership = _head_ownership(
            api,
            repository=repository,
            head_sha=candidate_head,
        )
    except review_policy.ReviewPolicyError as error:
        return f"pending for {candidate_head}: {error}"
    pull_request = ownership.pull_request
    if pull_request is None:
        trusted_no_owner = source.action == "closed" or ownership.association_count > 0
        if not trusted_no_owner:
            return f"pending for {candidate_head}: no open pull request owns the head"
        adjusted_state = _capacity_adjusted_state(
            api,
            repository=repository,
            head_sha=candidate_head,
            target_url=target_url,
            state="success",
            result="no open pull request owns the affected head",
        )
        if adjusted_state is None:
            return f"pending for {candidate_head}: maintainer approval status capacity is exhausted"
        try:
            final_ownership = _head_ownership(
                api,
                repository=repository,
                head_sha=candidate_head,
            )
        except review_policy.ReviewPolicyError as error:
            return f"pending for {candidate_head}: {error}"
        if final_ownership.pull_request is not None:
            return f"pending for {candidate_head}: pull request ownership changed"
        if source.action != "closed" and final_ownership.association_count == 0:
            return f"pending for {candidate_head}: pull request association changed"
        state, result = adjusted_state
        _publish_status(
            api,
            repository=repository,
            head_sha=candidate_head,
            state=state,
            description=result,
            target_url=target_url,
        )
        return f"{state} for {candidate_head}: {result}"

    # Bracket the authoritative policy read with exact-head ownership checks.
    _policy_state(api, repository=repository, pull_request=pull_request)
    try:
        confirmed = _head_ownership(
            api,
            repository=repository,
            head_sha=candidate_head,
        )
    except review_policy.ReviewPolicyError as error:
        return f"pending for {candidate_head}: {error}"
    if not _same_candidate(pull_request, confirmed.pull_request):
        return f"pending for {candidate_head}: pull request ownership changed"

    state, result = _policy_state(
        api,
        repository=repository,
        pull_request=pull_request,
    )
    try:
        final_confirmation = _head_ownership(
            api,
            repository=repository,
            head_sha=candidate_head,
        )
    except review_policy.ReviewPolicyError as error:
        return f"pending for {candidate_head}: {error}"
    if not _same_candidate(pull_request, final_confirmation.pull_request):
        return f"pending for {candidate_head}: pull request ownership changed"

    adjusted_state = _capacity_adjusted_state(
        api,
        repository=repository,
        head_sha=candidate_head,
        target_url=target_url,
        state=state,
        result=result,
    )
    if adjusted_state is None:
        return f"pending for {candidate_head}: maintainer approval status capacity is exhausted"
    state, result = adjusted_state

    if state == "success":
        state, result = _policy_state(
            api,
            repository=repository,
            pull_request=pull_request,
        )
    try:
        post_policy_confirmation = _head_ownership(
            api,
            repository=repository,
            head_sha=candidate_head,
        )
    except review_policy.ReviewPolicyError as error:
        return f"pending for {candidate_head}: {error}"
    if not _same_candidate(pull_request, post_policy_confirmation.pull_request):
        return f"pending for {candidate_head}: pull request ownership changed"

    _publish_status(
        api,
        repository=repository,
        head_sha=candidate_head,
        state=state,
        description=result,
        target_url=target_url,
    )
    return f"{state} for {candidate_head}: {result}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--source-workflow-run-id", type=int, required=True)
    parser.add_argument("--publisher-workflow-run-id", type=int, required=True)
    parser.add_argument("--publisher-workflow-run-attempt", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-previous-head", required=True)
    parser.add_argument("--expected-source-action", required=True)
    parser.add_argument("--candidate-head", required=True)
    parser.add_argument("--server-url", default=os.environ.get("GITHUB_SERVER_URL"))
    parser.add_argument("--request-timeout", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Publish the status and return nonzero only when publication cannot fail closed."""
    args = _parser().parse_args(argv)
    try:
        if args.repository is None:
            raise review_policy.ReviewPolicyError("--repository or GITHUB_REPOSITORY is required")
        if args.server_url is None:
            raise review_policy.ReviewPolicyError("--server-url or GITHUB_SERVER_URL is required")
        api = review_policy.GitHubRestApi(
            os.environ.get("GITHUB_TOKEN", ""),
            base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            request_timeout=args.request_timeout,
        )
        result = publish_maintainer_approval(
            api,
            repository=args.repository,
            source_workflow_run_id=args.source_workflow_run_id,
            publisher_workflow_run_id=args.publisher_workflow_run_id,
            publisher_workflow_run_attempt=args.publisher_workflow_run_attempt,
            server_url=args.server_url,
            expected_head=args.expected_head,
            expected_previous_head=args.expected_previous_head,
            expected_source_action=args.expected_source_action,
            candidate_head=args.candidate_head,
        )
    except review_policy.ReviewPolicyError as error:
        print(f"maintainer approval publication failed: {error}", file=sys.stderr)
        return 1
    print(f"maintainer approval publication: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
