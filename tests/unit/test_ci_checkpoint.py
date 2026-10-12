"""Qualification follows current CI attempts without hiding real failures."""

import json
import subprocess

import pytest

from scripts import wait_for_ci_checkpoint as checkpoint
from scripts.wait_for_ci_checkpoint import CheckpointError, inspect_checkpoint, wait_for_checkpoint

SHA = "a" * 40
REPO = "owner/repo"


def run(number=2, **changes):
    return {
        "id": number,
        "run_number": number,
        "run_attempt": 1,
        "head_sha": SHA,
        "path": ".github/workflows/ci.yml",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        **changes,
    }


def api_for(runs, *, gate="success", confirmed=None):
    reads = []

    def api(endpoint):
        reads.append(endpoint)
        if "/jobs?" in endpoint:
            return {"total_count": 1, "jobs": [{"name": "CI Gate", "conclusion": gate}]}
        return {
            "total_count": len(runs),
            "workflow_runs": confirmed if confirmed is not None and len(reads) > 1 else runs,
        }

    return api, reads


def test_old_failed_gate_cannot_override_new_workflow_before_aggregate_exists():
    api, reads = api_for([run(1, conclusion="failure"), run(status="in_progress", conclusion=None)])
    assert inspect_checkpoint(api, REPO, SHA) is None
    assert len(reads) == 1


def test_current_attempt_is_checked_instead_of_earlier_green_attempt():
    api, reads = api_for([run(run_attempt=3)])
    result = inspect_checkpoint(api, REPO, SHA)
    assert result is not None
    assert result.run_id == 2 and result.attempt == 3
    assert "/attempts/3/jobs?" in reads[1]


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "skipped", "neutral"])
def test_real_current_failure_cannot_fall_back_to_historical_success(conclusion):
    api, _ = api_for([run(1), run(conclusion=conclusion)])
    with pytest.raises(CheckpointError, match="did not pass"):
        inspect_checkpoint(api, REPO, SHA)


@pytest.mark.parametrize(
    "change",
    [
        {"head_sha": "b" * 40},
        {"path": ".github/workflows/other.yml"},
        {"event": "pull_request_target"},
    ],
)
def test_unrelated_or_untrusted_workflow_cannot_satisfy_checkpoint(change):
    api, _ = api_for([run(**change)])
    assert inspect_checkpoint(api, REPO, SHA) is None


def test_replacement_appearing_during_inspection_must_be_waited_for():
    api, _ = api_for([run()], confirmed=[run(3, status="queued", conclusion=None)])
    assert inspect_checkpoint(api, REPO, SHA) is None


@pytest.mark.parametrize(
    "runs", [[], [run(conclusion="cancelled")], [run(status="queued", conclusion=None)]]
)
def test_missing_cancelled_and_pending_runs_have_a_finite_deadline(runs):
    api, _ = api_for(runs)
    now = [0]

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(CheckpointError, match="deadline"):
        wait_for_checkpoint(
            api, REPO, SHA, timeout=7, interval=5, clock=lambda: now[0], sleep=sleep
        )
    assert now[0] == 7


def test_workflow_success_without_passing_gate_is_rejected():
    api, _ = api_for([run()], gate="skipped")
    with pytest.raises(CheckpointError, match="unique passing"):
        inspect_checkpoint(api, REPO, SHA)


def test_success_arriving_after_deadline_is_not_accepted():
    now = [0]
    api, _ = api_for([run()])

    def slow_api(endpoint):
        now[0] += 4
        return api(endpoint)

    with pytest.raises(CheckpointError, match="deadline"):
        wait_for_checkpoint(
            slow_api, REPO, SHA, timeout=7, clock=lambda: now[0], sleep=lambda _: None
        )


@pytest.mark.parametrize("gates", [[], [{"name": "CI Gate", "conclusion": "success"}] * 2])
def test_missing_or_duplicate_gate_cannot_prove_acceptance(gates):
    api, _ = api_for([run()])

    def malformed(endpoint):
        return {"total_count": len(gates), "jobs": gates} if "/jobs?" in endpoint else api(endpoint)

    with pytest.raises(CheckpointError, match="unique passing"):
        inspect_checkpoint(malformed, REPO, SHA)


def test_truncated_run_inventory_is_rejected():
    with pytest.raises(CheckpointError, match="inventory"):
        inspect_checkpoint(lambda _: {"total_count": 101, "workflow_runs": [run()]}, REPO, SHA)


CANARY = "untrusted-secret-header-body-stderr"


def response(status=200, body=None, *, returncode=None, newline=b"\r\n"):
    if body is None:
        body = {"total_count": 1, "workflow_runs": [run()]}
    if not isinstance(body, bytes):
        body = json.dumps(body).encode()
    raw = newline.join(
        [
            f"HTTP/2.0 {status} {CANARY}".encode(),
            f"Sensitive: {CANARY}".encode(),
            b"",
            body,
        ]
    )
    return subprocess.CompletedProcess(
        ["gh"],
        (0 if status == 200 else 1) if returncode is None else returncode,
        stdout=raw,
        stderr=CANARY.encode(),
    )


@pytest.mark.parametrize("newline", [b"\r\n", b"\n"])
def test_metadata_uses_explicit_get_real_status_and_keeps_captured_material_private(
    monkeypatch, capsys, newline
):
    calls = []

    def invoke(command, **options):
        calls.append((command, options))
        return response(newline=newline)

    monkeypatch.setattr(checkpoint.subprocess, "run", invoke)
    assert checkpoint.read_metadata("fixed-endpoint", timeout=7) == {
        "total_count": 1,
        "workflow_runs": [run()],
    }
    assert calls == [
        (
            ["gh", "api", "--include", "--method", "GET", "fixed-endpoint"],
            {
                "capture_output": True,
                "check": False,
                "timeout": 7,
            },
        )
    ]
    assert capsys.readouterr() == ("", "")


def test_lf_headers_end_before_crlf_blank_lines_in_json_body(monkeypatch):
    reply = response(newline=b"\n", body=b'{\r\n\r\n"total_count":0, "workflow_runs":[]\r\n}')
    monkeypatch.setattr(checkpoint.subprocess, "run", lambda *a, **k: reply)
    assert checkpoint.read_metadata("fixed-endpoint", timeout=20) == {
        "total_count": 0,
        "workflow_runs": [],
    }


@pytest.mark.parametrize("status", [408, 429, 502, 503, 504])
def test_only_allowlisted_real_http_failures_are_retryable(monkeypatch, status):
    monkeypatch.setattr(checkpoint.subprocess, "run", lambda *a, **k: response(status))
    with pytest.raises(checkpoint.MetadataReadError) as caught:
        checkpoint.read_metadata("fixed-endpoint", timeout=100)
    assert caught.value.category == "http_transient"
    assert caught.value.status == status
    assert caught.value.retryable
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize(
    "status,category",
    [
        (401, "http_permission"),
        (403, "http_permission"),
        (404, "http_rejected"),
        (500, "http_rejected"),
        (501, "http_rejected"),
        (302, "http_rejected"),
    ],
)
def test_other_real_http_failures_are_not_retried(monkeypatch, status, category):
    monkeypatch.setattr(checkpoint.subprocess, "run", lambda *a, **k: response(status))
    with pytest.raises(checkpoint.MetadataReadError) as caught:
        checkpoint.read_metadata("fixed-endpoint", timeout=20)
    assert caught.value.category == category
    assert caught.value.status is None and not caught.value.retryable
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize(
    "error,retryable,category",
    [
        (subprocess.TimeoutExpired(CANARY, 20, output=CANARY, stderr=CANARY), True, "timeout"),
        (FileNotFoundError(CANARY), False, "command"),
        (PermissionError(CANARY), False, "command"),
        (subprocess.CalledProcessError(1, CANARY, output=CANARY, stderr=CANARY), False, "command"),
    ],
)
def test_transport_failures_have_fixed_categories(monkeypatch, error, retryable, category):
    def invoke(*args, **kwargs):
        raise error

    monkeypatch.setattr(checkpoint.subprocess, "run", invoke)
    with pytest.raises(checkpoint.MetadataReadError) as caught:
        checkpoint.read_metadata("fixed-endpoint", timeout=20)
    assert caught.value.category == category and caught.value.retryable is retryable
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize(
    "reply,category",
    [
        (response(body=b"not-json-" + CANARY.encode()), "malformed_response"),
        (response(body=[]), "malformed_contract"),
        (response(returncode=1), "command"),
        (subprocess.CompletedProcess(["gh"], 1, stdout=b"", stderr=b"(HTTP 502)"), "command"),
        (
            subprocess.CompletedProcess(["gh"], 0, stdout=CANARY.encode(), stderr=b""),
            "malformed_response",
        ),
    ],
)
def test_malformed_or_arbitrary_command_output_cannot_request_retry(monkeypatch, reply, category):
    monkeypatch.setattr(checkpoint.subprocess, "run", lambda *a, **k: reply)
    with pytest.raises(checkpoint.MetadataReadError) as caught:
        checkpoint.read_metadata("fixed-endpoint", timeout=20)
    assert caught.value.category == category and not caught.value.retryable
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"total_count": True, "workflow_runs": []},
        {"total_count": 1, "workflow_runs": []},
        {"total_count": 1, "workflow_runs": [CANARY]},
        {"total_count": 1, "workflow_runs": [run(id=True)]},
        {"total_count": 1, "workflow_runs": [run(run_attempt=0)]},
    ],
)
def test_malformed_contracts_refuse_immediately_without_sleep(payload):
    reads = []

    def api(endpoint):
        reads.append(endpoint)
        return payload

    with pytest.raises(checkpoint.MetadataReadError, match="malformed_contract"):
        wait_for_checkpoint(api, REPO, SHA, sleep=lambda _: pytest.fail("must not retry"))
    assert len(reads) == 1


@pytest.mark.parametrize("failed_read", [1, 2, 3])
def test_transient_read_restarts_whole_inspection_without_reusing_prior_green(failed_read, capsys):
    reads = []
    now = [0.0]

    def api(endpoint):
        reads.append(endpoint)
        if len(reads) == failed_read:
            raise checkpoint.MetadataReadError("http_transient", 502)
        if "/jobs?" in endpoint:
            return {"total_count": 1, "jobs": [{"name": "CI Gate", "conclusion": "success"}]}
        # The replacement attempt appears after the transient failure.
        return {
            "total_count": 1,
            "workflow_runs": [run(run_attempt=2 if len(reads) > failed_read else 1)],
        }

    result = wait_for_checkpoint(
        api, REPO, SHA, clock=lambda: now[0], sleep=lambda value: now.__setitem__(0, now[0] + value)
    )
    assert result == checkpoint.Checkpoint(2, 2)
    assert now[0] == 25
    assert "/workflows/ci.yml/runs?" in reads[failed_read]
    assert "/attempts/2/jobs?" in reads[-2]
    assert json.loads(capsys.readouterr().out) == {
        "status": "retrying",
        "checkpoint": "ci_metadata",
        "category": "http_transient",
        "http_status": 502,
        "retry": 1,
        "retry_limit": 3,
    }


@pytest.mark.parametrize(
    "replacement",
    [
        run(3, conclusion="failure"),
        run(3, status="in_progress", conclusion=None),
    ],
)
def test_transient_confirmation_never_accepts_old_green_over_new_current_run(replacement):
    calls = [0]
    now = [0.0]
    initial, _ = api_for([run()])

    def api(endpoint):
        calls[0] += 1
        if calls[0] == 3:
            raise checkpoint.MetadataReadError("timeout")
        if calls[0] > 3:
            return {"total_count": 1, "workflow_runs": [replacement]}
        return initial(endpoint)

    with pytest.raises(CheckpointError, match="did not pass|deadline"):
        wait_for_checkpoint(
            api,
            REPO,
            SHA,
            timeout=30,
            clock=lambda: now[0],
            sleep=lambda value: now.__setitem__(0, now[0] + value),
        )


def test_three_retry_limit_is_global_across_pending_polls_and_endpoints(capsys):
    calls = [0]
    now = [0.0]
    successes, _ = api_for([run()])

    def api(endpoint):
        calls[0] += 1
        if calls[0] in {1, 4, 7, 9}:
            raise checkpoint.MetadataReadError("http_transient", 503)
        if calls[0] in {2, 8}:
            return {"total_count": 1, "workflow_runs": [run(status="in_progress", conclusion=None)]}
        return successes(endpoint)

    with pytest.raises(CheckpointError, match="retry limit exhausted"):
        wait_for_checkpoint(
            api,
            REPO,
            SHA,
            clock=lambda: now[0],
            sleep=lambda value: now.__setitem__(0, now[0] + value),
        )
    assert calls[0] == 9 and now[0] == 125
    assert [json.loads(line)["retry"] for line in capsys.readouterr().out.splitlines()] == [1, 2, 3]


@pytest.mark.parametrize("confirm_timeout", [False, True])
def test_main_shares_one_deadline_and_caps_each_read_to_remaining_time(
    monkeypatch, capsys, confirm_timeout
):
    now = [0.0]
    calls = []
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("SOURCE_SHA", SHA)
    monkeypatch.setattr(checkpoint.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(checkpoint.time, "sleep", lambda value: now.__setitem__(0, now[0] + value))

    def invoke(command, **options):
        calls.append(options["timeout"])
        if len(calls) < 24:
            now[0] += options["timeout"]
            return response(
                body={
                    "total_count": 1,
                    "workflow_runs": [run(status="in_progress", conclusion=None)],
                }
            )
        now[0] += options["timeout"]
        if len(calls) == 26 and confirm_timeout:
            raise subprocess.TimeoutExpired(CANARY, options["timeout"], stderr=CANARY)
        if "/jobs?" in command[-1]:
            return response(
                body={"total_count": 1, "jobs": [{"name": "CI Gate", "conclusion": "success"}]}
            )
        return response()

    monkeypatch.setattr(checkpoint.subprocess, "run", invoke)
    assert checkpoint.main() == 1
    assert now[0] == 1080
    assert all(0 < value <= 20 for value in calls)
    assert calls[-1] == 5
    output = capsys.readouterr().out
    assert "deadline" in output and "retrying" not in output and CANARY not in output


def test_timeout_retry_cannot_extend_deadline_or_emit_captured_material(monkeypatch, capsys):
    now = [0.0]
    calls = []
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("SOURCE_SHA", SHA)
    monkeypatch.setattr(checkpoint.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(checkpoint.time, "sleep", lambda value: now.__setitem__(0, now[0] + value))

    def invoke(command, **options):
        calls.append(options["timeout"])
        if len(calls) < 24:
            now[0] += 20
            return response(
                body={
                    "total_count": 1,
                    "workflow_runs": [run(status="in_progress", conclusion=None)],
                }
            )
        now[0] += options["timeout"]
        raise subprocess.TimeoutExpired(CANARY, options["timeout"], output=CANARY, stderr=CANARY)

    monkeypatch.setattr(checkpoint.subprocess, "run", invoke)
    assert checkpoint.main() == 1
    assert now[0] == 1080
    output = capsys.readouterr().out
    assert "deadline" in output and CANARY not in output
    assert calls[-1] <= 20


@pytest.mark.parametrize("repo,sha", [("invalid", SHA), (REPO, "not-a-source")])
def test_main_cannot_bypass_repository_or_source_guard(monkeypatch, capsys, repo, sha):
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    monkeypatch.setenv("SOURCE_SHA", sha)
    monkeypatch.setattr(
        checkpoint.subprocess, "run", lambda *a, **k: pytest.fail("must not read API")
    )
    assert checkpoint.main() == 1
    assert "Invalid" in capsys.readouterr().out


@pytest.mark.parametrize(
    "reply",
    [
        response(401),
        response(403),
        response(404),
        response(500),
        response(body=CANARY.encode()),
        response(body={"total_count": CANARY, "workflow_runs": []}),
    ],
)
def test_main_refuses_permanent_failures_once_without_printing_captured_bytes(
    monkeypatch, capsys, reply
):
    calls = []
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("SOURCE_SHA", SHA)
    monkeypatch.setattr(checkpoint.time, "sleep", lambda _: pytest.fail("must not retry"))

    def invoke(*args, **kwargs):
        calls.append(args)
        return reply

    monkeypatch.setattr(checkpoint.subprocess, "run", invoke)
    assert checkpoint.main() == 1 and len(calls) == 1
    output = capsys.readouterr()
    assert output.out.startswith("::error::Unable to read CI checkpoint metadata (")
    assert CANARY not in output.out and output.err == ""
