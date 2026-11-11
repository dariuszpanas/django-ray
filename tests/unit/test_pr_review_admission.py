"""External PRs cannot obtain an automated formal review from a clean bot verdict."""

from __future__ import annotations

import copy
import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import check_pr_review_admission as admission

REPOSITORY = "dariuszpanas/django-ray"
REPOSITORY_ID = 123456
NUMBER = 42
HEAD = "a" * 40


@pytest.fixture
def pull_request():
    repository = {
        "id": REPOSITORY_ID,
        "full_name": REPOSITORY,
        "default_branch": "main",
        "owner": {"id": admission.OWNER_USER_ID, "type": "User"},
    }
    return {
        "number": NUMBER,
        "state": "open",
        "draft": False,
        "user": {"id": admission.OWNER_USER_ID, "type": "User", "login": "dariuszpanas"},
        "base": {"ref": "main", "repo": repository},
        "head": {"sha": HEAD, "repo": copy.deepcopy(repository)},
    }


def evaluate(value):
    return admission.evaluate(
        value, repository=REPOSITORY, repository_id=REPOSITORY_ID, number=NUMBER
    )


def test_owner_same_repository_is_admitted(pull_request):
    assert evaluate(pull_request) == admission.Admission(True, "owner-same-repository", HEAD)


@pytest.mark.parametrize(
    "path, value, reason",
    [
        (("user", "id"), 999, "external-author"),
        (("user", "type"), "Bot", "external-author"),
        (("head", "repo", "id"), 999, "external-source-branch"),
        (("head", "repo", "full_name"), "someone/django-ray", "external-source-branch"),
        (("head", "repo"), None, "external-source-branch"),
        (("state",), "closed", "inactive-pull-request"),
        (("draft",), True, "inactive-pull-request"),
        (("draft",), None, "inactive-pull-request"),
        (("number",), True, "unverified-metadata"),
        (("number",), NUMBER + 1, "unverified-metadata"),
        (("head", "sha"), "not-a-commit", "unverified-metadata"),
        (("base", "ref"), "release", "unverified-metadata"),
        (("base", "repo", "id"), 999, "unverified-metadata"),
        (("base", "repo", "owner", "id"), 999, "unverified-metadata"),
        (("base", "repo", "owner", "type"), "Organization", "unverified-metadata"),
        (("base", "repo", "full_name"), "someone/django-ray", "unverified-metadata"),
    ],
)
def test_untrusted_or_stale_metadata_is_held(pull_request, path, value, reason):
    target = pull_request
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    result = evaluate(pull_request)
    assert not result.allowed
    assert result.reason == reason


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"])
def test_login_association_and_prior_bot_approval_cannot_grant_admission(pull_request, association):
    pull_request["user"]["id"] = 999
    pull_request["author_association"] = association
    pull_request["reviews"] = [{"state": "APPROVED", "user": {"login": "github-actions[bot]"}}]
    assert evaluate(pull_request).reason == "external-author"


@pytest.mark.parametrize("value", [None, [], "approve", {"head": []}, {"head": {"sha": HEAD}}])
def test_incomplete_payload_fails_closed(value):
    assert not evaluate(value).allowed


@pytest.fixture
def workflow_context(monkeypatch, tmp_path):
    output, summary = tmp_path / "output", tmp_path / "summary"
    for key, value in {
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REPOSITORY_ID": str(REPOSITORY_ID),
        "PR_NUMBER": str(NUMBER),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }.items():
        monkeypatch.setenv(key, value)
    return output, summary


def test_each_publisher_invocation_reads_current_api_metadata(
    monkeypatch, pull_request, workflow_context, capsys
):
    requests = []

    def fetch(command, **kwargs):
        requests.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps(pull_request).encode())

    monkeypatch.setattr(subprocess, "run", fetch)
    assert admission.main() == 0
    pull_request["user"]["id"] = 999
    pull_request["body"] = "PRIVATE_PAYLOAD_DO_NOT_PRINT"
    pull_request["head"]["sha"] = "b" * 40
    assert admission.main() == 0
    output, summary = workflow_context
    assert (
        requests
        == [
            (
                ["gh", "api", "--method", "GET", f"repos/{REPOSITORY}/pulls/{NUMBER}"],
                {"check": True, "capture_output": True, "timeout": 20},
            )
        ]
        * 2
    )
    assert output.read_text().splitlines() == [
        "allowed=true",
        "reason=owner-same-repository",
        f"head_sha={HEAD}",
        "allowed=false",
        "reason=external-author",
        f"head_sha={'b' * 40}",
    ]
    assert "Automatic review withheld" in summary.read_text()
    assert "PRIVATE_PAYLOAD" not in summary.read_text() + capsys.readouterr().out


@pytest.mark.parametrize("response", [b"invalid JSON", b"x" * (admission.MAX_RESPONSE_BYTES + 1)])
def test_invalid_api_response_blocks_publication(monkeypatch, response, workflow_context, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=response))
    assert admission.main() == 1
    assert not workflow_context[0].exists()
    assert "could not be verified" in capsys.readouterr().err


def test_api_failure_does_not_print_response_body(monkeypatch, workflow_context, capsys):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["gh"], stderr=b"PRIVATE_RESPONSE")

    monkeypatch.setattr(subprocess, "run", fail)
    assert admission.main() == 1
    assert not workflow_context[0].exists()
    assert "PRIVATE_RESPONSE" not in capsys.readouterr().err


def test_invalid_workflow_target_never_calls_api(monkeypatch, workflow_context):
    monkeypatch.setenv("PR_NUMBER", "42; echo injected")

    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid target must be rejected before invoking gh")

    monkeypatch.setattr(subprocess, "run", unexpected)
    assert admission.main() == 1
    assert not workflow_context[0].exists()
