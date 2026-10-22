"""Synthetic GitHub/ZIP boundary checks; no network, jobs or qualification execution."""

import copy
import hashlib
import io
import json
import stat
import struct
import zipfile
from dataclasses import replace

import pytest

from scripts import test_planning_acquisition as acquisition
from scripts.test_planning_results import Cell, ExpectedRun, canonical, normalize_results

SOURCE = "a" * 40
TREE = "b" * 40
RUN = "1234"
START = "2026-10-18T10:00:00Z"
UPLOAD_START = "2026-10-18T10:09:00Z"
UPLOADED = "2026-10-18T10:09:01Z"
END = "2026-10-18T10:10:00Z"
CELL = Cell("qualification.latency", "default", "latency-qualification")


def expected(cells=(CELL,)):
    return ExpectedRun("c" * 64, "d" * 64, "e" * 64, SOURCE, TREE, RUN, 1, cells)


def zip_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


class GithubFixture:
    def __init__(self, cells=(CELL,)):
        self.calls = []
        self.replies = {}
        prefix = acquisition.PREFIX
        run_route = f"{prefix}/actions/runs/{RUN}"
        repository = {"id": acquisition.REPOSITORY_ID, "full_name": acquisition.REPOSITORY}
        self.run = {
            "id": int(RUN),
            "run_attempt": 1,
            "workflow_id": acquisition.WORKFLOW_ID,
            "path": acquisition.WORKFLOW,
            "name": acquisition.WORKFLOW_NAME,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": SOURCE,
            "repository": repository,
            "head_repository": repository,
            "head_commit": {"id": SOURCE, "tree_id": TREE},
            "status": "in_progress",
            "created_at": START,
            "run_started_at": START,
            "referenced_workflows": [
                {
                    "path": f"{acquisition.REPOSITORY}/.github/workflows/{name}-qualification.yml@{SOURCE}",
                    "sha": SOURCE,
                    "ref": "refs/heads/main",
                }
                for name in acquisition.WORKFLOWS
            ],
        }
        self.replies[prefix] = repository
        self.replies[prefix + "/actions/workflows/test-planning-shadow.yml"] = {
            "id": acquisition.WORKFLOW_ID,
            "path": acquisition.WORKFLOW,
            "name": acquisition.WORKFLOW_NAME,
        }
        self.replies[prefix + "/git/commits/" + SOURCE] = {"sha": SOURCE, "tree": {"sha": TREE}}
        self.replies[run_route] = self.run
        self.replies[run_route + "/attempts/1"] = self.run
        self.jobs, self.artifacts = [], []
        for index, cell in enumerate(cells, 1):
            job_id, artifact_id = 100 + index, 200 + index
            names = (
                *acquisition.REQUIRED_STEPS[cell.category],
                acquisition.UPLOAD_STEPS[cell.category],
            )
            job = {
                "id": job_id,
                "name": f"native / Upgrade Native ({cell.variant})"
                if cell.category == "native"
                else acquisition.JOB_NAMES[cell.category],
                "run_id": int(RUN),
                "run_attempt": 1,
                "head_sha": SOURCE,
                "head_branch": "main",
                "workflow_name": acquisition.WORKFLOW_NAME,
                "status": "completed",
                "conclusion": "success",
                "started_at": START,
                "completed_at": END,
                "steps": [
                    {
                        "name": name,
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": UPLOAD_START if number == len(names) - 1 else START,
                        "completed_at": END,
                    }
                    for number, name in enumerate(names)
                ],
            }
            name = acquisition.artifact_name(cell, RUN)
            raw = zip_bytes(
                [(path, b"fixed fixture data") for path in sorted(acquisition.receipt_paths(cell))]
            )
            artifact = {
                "id": artifact_id,
                "name": name,
                "expired": False,
                "size_in_bytes": len(raw),
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "created_at": UPLOADED,
                "updated_at": UPLOADED,
                "workflow_run": {
                    "id": int(RUN),
                    "repository_id": acquisition.REPOSITORY_ID,
                    "head_repository_id": acquisition.REPOSITORY_ID,
                    "head_sha": SOURCE,
                    "head_branch": "main",
                },
            }
            logs = f"{UPLOADED} SHA256 digest of uploaded artifact is {artifact['digest'][7:]}\n{UPLOADED} Artifact {name} has been successfully uploaded! Final size is {len(raw)} bytes. Artifact ID is {artifact_id}\n".encode()
            self.jobs.append(job)
            self.artifacts.append(artifact)
            self.replies[f"{prefix}/actions/jobs/{job_id}"] = job
            self.replies[f"{prefix}/actions/jobs/{job_id}/logs"] = logs
            self.replies[f"{prefix}/actions/artifacts/{artifact_id}"] = artifact
            self.replies[f"{prefix}/actions/artifacts/{artifact_id}/zip"] = raw
        self.jobs_route = run_route + "/attempts/1/jobs?per_page=100&page=1"
        self.artifacts_route = run_route + "/artifacts?per_page=100&page=1"
        self.replies[self.jobs_route] = {"total_count": len(self.jobs), "jobs": self.jobs}
        self.replies[self.artifacts_route] = {
            "total_count": len(self.artifacts),
            "artifacts": self.artifacts,
        }

    def get(self, endpoint, maximum):
        assert acquisition._endpoint(endpoint)
        self.calls.append(endpoint)
        reply = self.replies[endpoint]
        if callable(reply):
            reply = reply()
        raw = reply if isinstance(reply, bytes) else canonical(reply)
        assert len(raw) <= maximum
        return raw


def test_acquisition_accepts_same_source_selected_jobs_while_parent_is_running(tmp_path):
    github = GithubFixture()
    result = acquisition.acquire_receipts(expected(), tmp_path / "observation", github=github)
    assert result.provenance.run_id == RUN and result.provenance.attempt == 1
    assert result.provenance.jobs[0].cell == CELL
    assert result.provenance.jobs[0].outcome == "passed"
    assert len(result.provenance.files) == 3
    assert result.evidence["selected_jobs"][0]["duration_seconds"] == 600
    assert result.evidence["workflow_created_at"] == START
    assert (tmp_path / "observation/archives/201.zip").read_bytes() == github.replies[
        acquisition.PREFIX + "/actions/artifacts/201/zip"
    ]
    assert (result.root / result.provenance.files[0].path).is_file()
    saved = json.loads((tmp_path / "observation/acquisition.json").read_bytes())
    assert saved == result.evidence


def test_empty_selection_is_authenticated_without_claiming_full_qualification(tmp_path):
    github = GithubFixture(())
    result = acquisition.acquire_receipts(expected(()), tmp_path / "empty", github=github)
    assert result.provenance.jobs == result.provenance.files == ()
    assert result.evidence["selected_jobs"] == result.evidence["artifacts"] == []
    assert acquisition.PREFIX + "/git/commits/" + SOURCE in github.calls


def test_all_eight_exact_cell_mappings_have_complete_fixed_receipt_inventory(tmp_path):
    cells = tuple(
        Cell("qualification." + category, variant, binding)
        for category, binding in {
            "application": "application-qualification",
            "native": "upgrade-native-qualification",
            "latency": "latency-qualification",
            "cancellation": "cancellation-qualification",
            "monitoring": "monitoring-qualification",
        }.items()
        for variant in (
            ("core-loss", "jobs", "jobs-crash", "native") if category == "native" else ("default",)
        )
    )
    result = acquisition.acquire_receipts(
        expected(cells), tmp_path / "all", github=GithubFixture(cells)
    )
    assert len(result.provenance.jobs) == 8 and len(result.provenance.files) == 53


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending"])
def test_pending_polls_only_small_jobs_inventory_and_create_no_directory(tmp_path, status):
    github = GithubFixture()
    github.jobs[0]["status"] = status
    with pytest.raises(acquisition.AcquisitionPending):
        acquisition.acquire_receipts(expected(), tmp_path / "pending", github=github)
    assert github.calls == [github.jobs_route]
    assert not (tmp_path / "pending").exists()


@pytest.mark.parametrize(
    "conclusion,outcome",
    [(key, value) for key, value in acquisition.OUTCOMES.items() if value != "passed"],
)
def test_nonpassing_selected_jobs_preserve_authenticated_outcomes_without_receipts(
    tmp_path, conclusion, outcome
):
    github = GithubFixture()
    github.jobs[0]["conclusion"] = conclusion
    github.jobs[0]["steps"] = []
    # A same-name stale success archive or forged log never participates.
    github.replies[acquisition.PREFIX + "/actions/artifacts/201/zip"] = b"untrusted"
    github.replies[acquisition.PREFIX + "/actions/jobs/101/logs"] = b"untrusted"
    result = acquisition.acquire_receipts(expected(), tmp_path / "failed", github=github)
    assert result.provenance.jobs[0].outcome == outcome
    assert result.provenance.jobs[0].successful_steps == ()
    assert result.provenance.files == ()
    assert result.evidence["artifacts"] == []
    job = result.evidence["selected_jobs"][0]
    assert job["github_conclusion"] == conclusion and job["outcome"] == outcome
    assert job["artifact"] is job["log_sha256"] is job["upload_completed_at"] is None
    assert not any(route.endswith(("/zip", "/logs")) for route in github.calls)
    assert not list((tmp_path / "failed/archives").iterdir())
    normalized = normalize_results(result.root, expected=expected(), provenance=result.provenance)
    assert normalized["results"][0]["outcome"] == outcome


@pytest.mark.parametrize("outcome", [None, "unknown", True, []])
def test_completed_unknown_conclusions_fail_closed(tmp_path, outcome):
    github = GithubFixture()
    github.jobs[0]["conclusion"] = outcome
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.acquire_receipts(expected(), tmp_path / "failed", github=github)
    assert github.calls == [github.jobs_route]


def test_terminal_unstarted_job_preserves_unknown_duration(tmp_path):
    github = GithubFixture()
    github.jobs[0].update(conclusion="skipped", started_at=None, steps=[])
    result = acquisition.acquire_receipts(expected(), tmp_path / "blocked", github=github)
    job = result.evidence["selected_jobs"][0]
    assert job["outcome"] == "blocked"
    assert job["started_at"] is job["duration_seconds"] is None
    assert job["completed_at"] == END


def test_mixed_terminal_results_keep_failure_and_only_success_artifacts(tmp_path):
    other = Cell("qualification.cancellation", "default", "cancellation-qualification")
    github = GithubFixture((CELL, other))
    github.jobs[0].update(conclusion="failure", steps=[])
    result = acquisition.acquire_receipts(
        expected((CELL, other)), tmp_path / "mixed", github=github
    )
    assert [job.outcome for job in result.provenance.jobs] == ["failed", "passed"]
    assert len(result.provenance.files) == 3
    assert [artifact["id"] for artifact in result.evidence["artifacts"]] == [202]


def test_nonpassing_original_job_source_and_reread_are_still_required(tmp_path):
    github = GithubFixture()
    github.jobs[0].update(conclusion="failure", steps=[])
    replies = [github.jobs[0], {**github.jobs[0], "run_attempt": 2}]
    github.replies[acquisition.PREFIX + "/actions/jobs/101"] = lambda: replies.pop(0)
    with pytest.raises(acquisition.AcquisitionError, match="job-changed-during-acquisition"):
        acquisition.acquire_receipts(expected(), tmp_path / "changed", github=github)


def test_failure_does_not_end_wait_while_another_selected_job_is_running(tmp_path):
    other = Cell("qualification.cancellation", "default", "cancellation-qualification")
    github = GithubFixture((CELL, other))
    github.jobs[0].update(conclusion="failure", steps=[])
    github.jobs[1].update(status="in_progress", conclusion=None)
    with pytest.raises(acquisition.AcquisitionPending):
        acquisition.acquire_receipts(expected((CELL, other)), tmp_path / "pending", github=github)
    assert github.calls == [github.jobs_route]
    assert not (tmp_path / "pending").exists()


@pytest.mark.parametrize("attempt", [0, True, 2, 17])
def test_reruns_rejected_before_network_even_if_success_rows_were_copied(tmp_path, attempt):
    github = GithubFixture()
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.acquire_receipts(
            replace(expected(), attempt=attempt), tmp_path / "rerun", github=github
        )
    assert github.calls == []


@pytest.mark.parametrize(
    "key,bad",
    [
        ("event", "pull_request"),
        ("head_sha", "f" * 40),
        ("run_attempt", 2),
        ("workflow_id", True),
        ("path", ".github/workflows/other.yml"),
        ("head_branch", "feature"),
        ("head_commit", {"id": SOURCE, "tree_id": "f" * 40}),
        ("repository", {"id": acquisition.REPOSITORY_ID, "full_name": "other/repo"}),
        ("referenced_workflows", []),
    ],
)
def test_run_source_workflow_repository_and_attempt_are_independently_bound(tmp_path, key, bad):
    github = GithubFixture()
    github.run[key] = bad
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.acquire_receipts(expected(), tmp_path / "bad", github=github)


@pytest.mark.parametrize(
    "key,bad",
    [
        ("run_attempt", 2),
        ("head_sha", "f" * 40),
        ("workflow_name", "Other"),
        ("id", True),
        ("started_at", "2026-10-18T11:00:00Z"),
    ],
)
def test_original_job_lineage_and_clock_are_bound(tmp_path, key, bad):
    github = GithubFixture()
    github.jobs[0][key] = bad
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.acquire_receipts(expected(), tmp_path / "bad", github=github)


@pytest.mark.parametrize("mutation", ["missing", "failed", "duplicate", "time"])
def test_required_cleanup_and_upload_steps_must_succeed_exactly_once(tmp_path, mutation):
    github = GithubFixture()
    steps = github.jobs[0]["steps"]
    if mutation == "missing":
        steps.pop()
    elif mutation == "failed":
        steps[-2]["conclusion"] = "failure"
    elif mutation == "duplicate":
        steps.append(copy.deepcopy(steps[-1]))
    else:
        steps[-1]["started_at"] = "2026-10-18T11:00:00Z"
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.acquire_receipts(expected(), tmp_path / "bad", github=github)


@pytest.mark.parametrize(
    "key,bad",
    [
        ("expired", True),
        ("digest", "sha256:" + "0" * 64),
        ("size_in_bytes", True),
        ("created_at", START),
        ("workflow_run", {"id": int(RUN) + 1}),
    ],
)
def test_artifact_api_identity_digest_size_and_upload_window_are_bound(tmp_path, key, bad):
    github = GithubFixture()
    github.artifacts[0][key] = bad
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.acquire_receipts(expected(), tmp_path / "bad", github=github)


def test_current_artifact_name_cannot_use_another_job_upload_log(tmp_path):
    github = GithubFixture()
    route = acquisition.PREFIX + "/actions/jobs/101/logs"
    github.replies[route] = github.replies[route].replace(
        b"Artifact ID is 201", b"Artifact ID is 202"
    )
    with pytest.raises(acquisition.AcquisitionError, match="artifact-upload-log-binding-missing"):
        acquisition.acquire_receipts(expected(), tmp_path / "bad", github=github)


def test_upload_log_keeps_last_second_precision_without_accepting_next_second():
    github = GithubFixture()
    raw = github.replies[acquisition.PREFIX + "/actions/jobs/101/logs"]
    assert isinstance(raw, bytes)
    artifact, upload = github.artifacts[0], github.jobs[0]["steps"][-1]
    # GitHub step metadata is second-resolution; actual logs have seven digits.
    last_second = raw.replace(UPLOADED.encode(), b"2026-10-18T10:10:00.9999999Z")
    acquisition.validate_upload_log(last_second, artifact, upload)
    with pytest.raises(acquisition.AcquisitionError, match="outside-upload-window"):
        acquisition.validate_upload_log(
            raw.replace(UPLOADED.encode(), b"2026-10-18T10:10:01Z"), artifact, upload
        )


def test_archive_bytes_must_match_server_digest(tmp_path):
    github = GithubFixture()
    route = acquisition.PREFIX + "/actions/artifacts/201/zip"
    original = github.replies[route]
    assert isinstance(original, bytes)
    github.replies[route] = b"0" + original[1:]
    with pytest.raises(acquisition.AcquisitionError, match="artifact-archive-digest-mismatch"):
        acquisition.acquire_receipts(expected(), tmp_path / "bad", github=github)


def test_run_identity_is_rechecked_before_provenance_is_returned(tmp_path):
    github = GithubFixture()
    route = acquisition.PREFIX + "/actions/runs/" + RUN
    replies = [github.run, {**github.run, "run_attempt": 2}]
    github.replies[route] = lambda: replies.pop(0)
    with pytest.raises(acquisition.AcquisitionError, match="wrong-shadow-run"):
        acquisition.acquire_receipts(expected(), tmp_path / "bad", github=github)


def test_run_clock_cannot_change_after_job_timing_was_validated(tmp_path):
    github = GithubFixture()
    route = acquisition.PREFIX + "/actions/runs/" + RUN
    replies = [github.run, {**github.run, "run_started_at": END}]
    github.replies[route] = lambda: replies.pop(0)
    with pytest.raises(acquisition.AcquisitionError, match="run-timestamps-changed"):
        acquisition.acquire_receipts(expected(), tmp_path / "bad", github=github)


def test_run_attempt_clock_must_match_run_clock(tmp_path):
    github = GithubFixture()
    route = acquisition.PREFIX + "/actions/runs/" + RUN + "/attempts/1"
    github.replies[route] = {**github.run, "run_started_at": END}
    with pytest.raises(acquisition.AcquisitionError, match="inconsistent-run-timestamps"):
        acquisition.acquire_receipts(expected(), tmp_path / "bad", github=github)


@pytest.mark.parametrize("field", ["total_count", "jobs"])
def test_incomplete_or_duplicated_api_inventory_is_not_a_partial_success(tmp_path, field):
    github = GithubFixture()
    if field == "total_count":
        github.replies[github.jobs_route][field] = 101
    else:
        github.replies[github.jobs_route][field].append(copy.deepcopy(github.jobs[0]))
        github.replies[github.jobs_route]["total_count"] = 2
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.acquire_receipts(expected(), tmp_path / "bad", github=github)


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "C:/drive",
        "a\\b",
        "a/../b",
        "a//b",
        "CON.txt",
        "a.",
        "a/./b",
        "a\x00hidden",
    ],
)
def test_archive_paths_are_rejected_before_any_extraction(path):
    # ZipInfo normalizes backslashes on Windows and truncates NUL while writing;
    # alter both stored names to exercise an actually hostile input archive.
    placeholder = path.replace("\\", "_").replace("\x00", "X")
    raw = zip_bytes([("receipt.json", b"{}"), (placeholder, b"bad")])
    if placeholder != path:
        raw = raw.replace(placeholder.encode(), path.encode())
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.extract_receipts(raw, frozenset({"receipt.json"}))


@pytest.mark.parametrize(
    "entries", [[("a", b"one"), ("A", b"two")], [("a", b"one"), ("a/b", b"two")]]
)
def test_archive_collisions_and_parent_file_conflicts_are_rejected(entries):
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.extract_receipts(zip_bytes(entries), frozenset({"a"}))


def test_archive_symlinks_are_not_data():
    member = zipfile.ZipInfo("receipt.json")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.extract_receipts(zip_bytes([(member, b"target")]), frozenset({"receipt.json"}))


@pytest.mark.parametrize("lie_about_count", [False, True])
def test_member_budget_is_enforced_before_zipfile_allocates_objects(monkeypatch, lie_about_count):
    raw = zip_bytes([(f"file-{index}", b"") for index in range(acquisition.MAX_MEMBERS + 1)])
    if lie_about_count:
        raw = bytearray(raw)
        struct.pack_into("<HH", raw, len(raw) - 22 + 8, 1, 1)
        raw = bytes(raw)

    def should_not_parse(*args, **kwargs):
        pytest.fail("zipfile must not parse an over-budget inventory")

    monkeypatch.setattr(zipfile, "ZipFile", should_not_parse)
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.extract_receipts(raw, frozenset({"file-0"}))


@pytest.mark.parametrize("mutation", ["multidisk", "zip64", "overlay", "directory-size"])
def test_unsupported_zip_metadata_fails_before_member_allocation(monkeypatch, mutation):
    raw = bytearray(zip_bytes([("receipt.json", b"{}")]))
    end = len(raw) - 22
    if mutation == "multidisk":
        struct.pack_into("<H", raw, end + 4, 1)
    elif mutation == "zip64":
        struct.pack_into("<HH", raw, end + 8, 65535, 65535)
    elif mutation == "overlay":
        raw.extend(b"extra")
    else:
        struct.pack_into("<I", raw, end + 12, acquisition.MAX_ZIP_DIRECTORY + 1)

    def should_not_parse(*args, **kwargs):
        pytest.fail("zipfile must not parse unsupported metadata")

    monkeypatch.setattr(zipfile, "ZipFile", should_not_parse)
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.extract_receipts(bytes(raw), frozenset({"receipt.json"}))


def test_missing_or_oversized_receipt_cannot_authorize_normalization():
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.extract_receipts(zip_bytes([("other", b"{}")]), frozenset({"receipt.json"}))
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.extract_receipts(
            zip_bytes([("receipt.json", b"0" * (acquisition.MAX_FILE_BYTES + 1))]),
            frozenset({"receipt.json"}),
        )


def test_publication_documents_can_explicitly_use_four_mib_file_limit():
    payload = b"0" * (acquisition.MAX_FILE_BYTES + 1)
    raw = zip_bytes([("plan.json", payload)])
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.extract_receipts(raw, frozenset({"plan.json"}))
    assert acquisition.extract_receipts(
        raw, frozenset({"plan.json"}), max_file_bytes=4 * acquisition.MIB
    ) == {"plan.json": payload}


@pytest.mark.parametrize("limit", [0, -1, True, None, "4194304", 1.0, 4 * acquisition.MIB + 1])
def test_extracted_file_limit_is_a_bounded_positive_integer(limit):
    with pytest.raises(acquisition.AcquisitionError, match="invalid-extracted-file-limit"):
        acquisition.extract_receipts(
            zip_bytes([("receipt.json", b"{}")]),
            frozenset({"receipt.json"}),
            max_file_bytes=limit,
        )


def test_larger_publication_file_limit_preserves_total_extraction_limit():
    payload = b"0" * (4 * acquisition.MIB)
    entries = [(f"plan-{index}.json", payload) for index in range(5)]
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.extract_receipts(
            zip_bytes(entries),
            frozenset(name for name, _ in entries),
            max_file_bytes=4 * acquisition.MIB,
        )


def test_output_directory_must_be_new(tmp_path):
    with pytest.raises(acquisition.AcquisitionError, match="output-directory-not-fresh"):
        acquisition.acquire_receipts(expected(), tmp_path, github=GithubFixture())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.invalid",
        "repos/other/repo/actions/runs/1",
        acquisition.PREFIX + "/issues",
        acquisition.PREFIX + "/actions/jobs/1/logs?token=secret",
        acquisition.PREFIX + "/actions/artifacts/1/zip/../../issues",
    ],
)
def test_github_transport_never_accepts_arbitrary_urls_or_routes(endpoint):
    assert not acquisition._endpoint(endpoint)


def test_github_transport_bounds_process_output_and_suppresses_stderr(monkeypatch):
    import subprocess

    calls = []

    class Process:
        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
            self.stdout = io.BytesIO(b"x" * 40)
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -1

        def wait(self, timeout):
            self.returncode = self.returncode or 0
            return self.returncode

    monkeypatch.setattr(acquisition.shutil, "which", lambda _: __file__)
    monkeypatch.setattr(subprocess, "Popen", Process)
    with pytest.raises(acquisition.AcquisitionError, match="github-response-byte-budget"):
        acquisition.Github().get(acquisition.PREFIX, 10)
    command, kwargs = calls[0]
    assert command[1:6] == ["api", "--hostname", "github.com", "--method", "GET"]
    assert kwargs["stderr"] is subprocess.DEVNULL and kwargs["shell"] is False


class PipeProcess:
    """A CLI process whose buffered stdout is never connected to the terminal."""

    def __init__(self, raw, returncode=0):
        self.stdout = io.BytesIO(raw)
        self.returncode = None
        self.final_code = returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -1

    def wait(self, timeout):
        if self.returncode is None:
            self.returncode = self.final_code
        return self.returncode


CLI_HELP = b"USAGE\n  gh api <endpoint> [flags]\nFLAGS\n"
ESCAPE_FLAG = b"      --allow-escape-sequences   Allow printing terminal escape sequences\n"
RAW_LOG = b"2026-10-18T00:00:00Z \x1b[36;1mprivate log\x1b[0m\n"
RAW_ZIP = b"PK\x03\x04\x00\xff\x1b\x00private archive"


@pytest.mark.parametrize("supports_flag", [False, True])
def test_raw_logs_use_one_cached_capability_probe_and_exact_private_bytes(
    monkeypatch, capsys, supports_flag
):
    commands = []

    def spawn(command, **kwargs):
        commands.append((command, kwargs))
        if command[1:] == ["api", "--help"]:
            return PipeProcess(CLI_HELP + (ESCAPE_FLAG if supports_flag else b""))
        endpoint = command[-1]
        option = "--allow-escape-sequences" in command
        if endpoint.endswith("/logs"):
            # Model modern gh: a pipe alone does not allow ANSI-bearing text.
            return PipeProcess(
                RAW_LOG if option or not supports_flag else b"",
                0 if option or not supports_flag else 1,
            )
        assert not option
        return PipeProcess(RAW_ZIP if endpoint.endswith("/zip") else b"{}")

    monkeypatch.setattr(acquisition.shutil, "which", lambda _: __file__)
    monkeypatch.setattr(acquisition.subprocess, "Popen", spawn)
    for name in ("GH_DEBUG", "GH_FORCE_TTY", "CLICOLOR_FORCE", "FORCE_COLOR"):
        monkeypatch.setenv(name, "unsafe override")
    monkeypatch.setenv("GH_PAGER", "untrusted pager")
    monkeypatch.setenv("PAGER", "untrusted pager")
    monkeypatch.setenv("GH_TOKEN", "PRIVATE_TOKEN_SENTINEL")
    client = acquisition.Github()
    assert client.get(acquisition.PREFIX, 10) == b"{}"
    assert len(commands) == 1  # Metadata-only polls need no capability process.
    for job in (1, 2):
        assert client.get(acquisition.PREFIX + f"/actions/jobs/{job}/logs", 100) == RAW_LOG
    assert client.get(acquisition.PREFIX + "/actions/artifacts/3/zip", 100) == RAW_ZIP
    assert client.calls == 4 and len(commands) == 5
    assert sum(command[1:] == ["api", "--help"] for command, _ in commands) == 1
    for command, kwargs in commands:
        assert kwargs["stdout"] is acquisition.subprocess.PIPE
        assert kwargs["stderr"] is acquisition.subprocess.DEVNULL
        assert kwargs["stdin"] is acquisition.subprocess.DEVNULL and kwargs["shell"] is False
        assert all(
            name not in kwargs["env"]
            for name in ("GH_DEBUG", "GH_FORCE_TTY", "CLICOLOR_FORCE", "FORCE_COLOR")
        )
        assert kwargs["env"]["GH_PAGER"] == kwargs["env"]["PAGER"] == "cat"
        assert kwargs["env"]["NO_COLOR"] == "1" and kwargs["env"]["CLICOLOR"] == "0"
        assert kwargs["env"]["GH_TOKEN"] == "PRIVATE_TOKEN_SENTINEL"
        assert "PRIVATE_TOKEN_SENTINEL" not in command
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "fault,code",
    [
        ("oversized", "github-cli-probe-byte-budget"),
        ("nonzero", "github-cli-probe-failed"),
        ("malformed", "github-cli-probe-failed"),
        ("missing", "github-cli-probe-failed"),
    ],
)
def test_cli_probe_failure_is_bounded_cached_and_never_dispatches_a_get(monkeypatch, fault, code):
    commands = []

    def spawn(command, **_kwargs):
        commands.append(command)
        assert command[1:] == ["api", "--help"]
        if fault == "missing":
            raise OSError("PRIVATE_EXCEPTION_SENTINEL")
        raw = (
            b"x" * 65537
            if fault == "oversized"
            else b"unexpected help"
            if fault == "malformed"
            else CLI_HELP
        )
        return PipeProcess(raw, 1 if fault == "nonzero" else 0)

    monkeypatch.setattr(acquisition.shutil, "which", lambda _: __file__)
    monkeypatch.setattr(acquisition.subprocess, "Popen", spawn)
    client = acquisition.Github()
    for _ in range(2):
        with pytest.raises(acquisition.AcquisitionError, match="^" + code + "$"):
            client.get(acquisition.PREFIX + "/actions/jobs/1/logs", 100)
    assert client.calls == 0 and len(commands) == 1


@pytest.mark.parametrize("remaining,expected_wait", [(2, 2), (240, 5)])
def test_cli_probe_timeout_uses_remaining_shared_deadline(monkeypatch, remaining, expected_wait):
    joins, commands = [], []

    class StalledThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout):
            joins.append(timeout)

        def is_alive(self):
            return True

    def spawn(command, **_kwargs):
        commands.append(command)
        return PipeProcess(b"")

    monkeypatch.setattr(acquisition.shutil, "which", lambda _: __file__)
    monkeypatch.setattr(acquisition.subprocess, "Popen", spawn)
    monkeypatch.setattr(acquisition.threading, "Thread", StalledThread)
    monkeypatch.setattr(acquisition.time, "monotonic", lambda: 10)
    client = acquisition.Github()
    client.deadline = 10 + remaining
    with pytest.raises(acquisition.AcquisitionError, match="^github-cli-probe-timeout$"):
        client.get(acquisition.PREFIX + "/actions/jobs/1/logs", 100)
    assert joins[0] == expected_wait and client.calls == 0 and len(commands) == 1


@pytest.mark.parametrize("exhausted", ["requests", "deadline"])
def test_exhausted_http_budget_does_not_start_a_capability_probe(monkeypatch, exhausted):
    monkeypatch.setattr(acquisition.shutil, "which", lambda _: __file__)
    monkeypatch.setattr(
        acquisition.subprocess,
        "Popen",
        lambda *_a, **_kw: pytest.fail("process after budget exhausted"),
    )
    monkeypatch.setattr(acquisition.time, "monotonic", lambda: 10)
    client = acquisition.Github()
    if exhausted == "requests":
        client.calls = 100
    else:
        client.deadline = 10
    with pytest.raises(acquisition.AcquisitionError, match="^acquisition-request-budget$"):
        client.get(acquisition.PREFIX + "/actions/jobs/1/logs", 100)


def test_failed_raw_log_get_is_not_retried_or_reissued_without_the_flag(monkeypatch):
    commands = []

    def spawn(command, **_kwargs):
        commands.append(command)
        if command[1:] == ["api", "--help"]:
            return PipeProcess(CLI_HELP + ESCAPE_FLAG)
        assert "--allow-escape-sequences" in command
        return PipeProcess(b"PRIVATE_RESPONSE_SENTINEL", 1)

    monkeypatch.setattr(acquisition.shutil, "which", lambda _: __file__)
    monkeypatch.setattr(acquisition.subprocess, "Popen", spawn)
    client = acquisition.Github()
    with pytest.raises(acquisition.AcquisitionError, match="^github-request-failed$"):
        client.get(acquisition.PREFIX + "/actions/jobs/1/logs", 100)
    assert client.calls == 1 and len(commands) == 2


def test_help_mentions_do_not_enable_an_unadvertised_option(monkeypatch):
    commands = []

    def spawn(command, **_kwargs):
        commands.append(command)
        if command[1:] == ["api", "--help"]:
            return PipeProcess(
                CLI_HELP + b"Description mentions --allow-escape-sequences, without declaring it.\n"
            )
        return PipeProcess(RAW_LOG)

    monkeypatch.setattr(acquisition.shutil, "which", lambda _: __file__)
    monkeypatch.setattr(acquisition.subprocess, "Popen", spawn)
    assert acquisition.Github().get(acquisition.PREFIX + "/actions/jobs/1/logs", 100) == RAW_LOG
    assert "--allow-escape-sequences" not in commands[-1]


@pytest.mark.parametrize("default_client", [False, True])
def test_diagnostics_observe_both_client_paths_without_changing_acquisition(
    tmp_path, monkeypatch, default_client
):
    github = GithubFixture()
    diagnostics = acquisition.AcquisitionDiagnostics()
    options = {} if default_client else {"github": github}
    monkeypatch.setattr(acquisition, "Github", lambda: github)
    result = acquisition.acquire_receipts(
        expected(), tmp_path / "observed", diagnostics=diagnostics, **options
    )
    report = diagnostics.snapshot()
    assert result.provenance.jobs[0].outcome == "passed"
    assert report == {
        "request_count": len(github.calls),
        "response_count": len(github.calls),
        "response_bytes": sum(
            len(raw if isinstance(raw, bytes) else canonical(raw))
            for raw in (github.replies[endpoint] for endpoint in github.calls)
        ),
        "counters_saturated": False,
        "last_route_kind": "run",
        "last_object_id": RUN,
        "last_response_bytes": len(canonical(github.run)),
        "last_response_sha256": hashlib.sha256(canonical(github.run)).hexdigest(),
    }
    assert len(canonical(report)) < 4096
    report["request_count"] = "private payload"
    assert diagnostics.snapshot()["request_count"] == len(github.calls)


def test_diagnostics_preserve_pending_and_terminal_failures_without_retry(tmp_path):
    github = GithubFixture()
    github.jobs[0]["status"] = "in_progress"
    diagnostics = acquisition.AcquisitionDiagnostics()
    destination = tmp_path / "pending"
    with pytest.raises(acquisition.AcquisitionPending):
        acquisition.acquire_receipts(
            expected(), destination, github=github, diagnostics=diagnostics
        )
    assert not destination.exists()
    assert github.calls == [github.jobs_route]
    assert diagnostics.snapshot()["last_route_kind"] == "run-jobs"
    assert diagnostics.snapshot()["response_count"] == 1
    error = acquisition.AcquisitionError("github-request-timeout")

    def failure():
        raise error

    github.replies[github.jobs_route] = failure
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.acquire_receipts(
            expected(), destination, github=github, diagnostics=diagnostics
        )
    assert caught.value is error
    assert not destination.exists()
    assert github.calls == [github.jobs_route] * 2
    report = diagnostics.snapshot()
    assert report["request_count"] == 2 and report["response_count"] == 1
    assert report["last_response_bytes"] is report["last_response_sha256"] is None


@pytest.mark.parametrize(
    "suffix,kind,object_id",
    [
        ("", "repository", None),
        ("/git/commits/" + SOURCE, "commit", None),
        ("/actions/workflows/test-planning-shadow.yml", "workflow", None),
        ("/actions/runs/1234", "run", "1234"),
        ("/actions/runs/1234/attempts/1", "run-attempt", "1234"),
        ("/actions/runs/1234/attempts/1/jobs?per_page=100&page=1", "run-jobs", "1234"),
        ("/actions/runs/1234/artifacts?per_page=100&page=1", "run-artifacts", "1234"),
        ("/actions/jobs/1234", "job", "1234"),
        ("/actions/jobs/1234/logs", "job-log", "1234"),
        ("/actions/artifacts/1234", "artifact", "1234"),
        ("/actions/artifacts/1234/zip", "artifact-archive", "1234"),
        ("/actions/jobs/9007199254740992", "job", None),
        ("/actions/jobs/0", "unsupported", None),
        ("/actions/jobs/1234/logs?token=private", "unsupported", None),
        ("/actions/jobs/" + "1" * 1000, "unsupported", None),
    ],
)
def test_diagnostic_routes_expose_only_fixed_kinds_and_portable_ids(suffix, kind, object_id):
    diagnostics = acquisition.AcquisitionDiagnostics()
    diagnostics.request(acquisition.PREFIX + suffix)
    report = diagnostics.snapshot()
    assert report["last_route_kind"] == kind
    assert report["last_object_id"] == object_id
    encoded = canonical(report)
    assert len(encoded) < 4096
    assert b"private" not in encoded and acquisition.PREFIX.encode() not in encoded


@pytest.mark.parametrize("payload,maximum", [(b"private" * 2, 10), (b"private", True)])
def test_diagnostics_do_not_fingerprint_responses_outside_the_response_boundary(payload, maximum):
    diagnostics = acquisition.AcquisitionDiagnostics()
    diagnostics.request(acquisition.PREFIX)
    diagnostics.response(payload, maximum)
    report = diagnostics.snapshot()
    assert report["request_count"] == 1
    assert report["response_count"] == report["response_bytes"] == 0
    assert report["last_response_bytes"] is report["last_response_sha256"] is None
    assert b"private" not in canonical(report)


def test_diagnostics_do_not_weaken_acquisition_response_limits(tmp_path):
    class OversizedClient:
        def get(self, endpoint, maximum):
            return b"x" * (maximum + 1)

    diagnostics = acquisition.AcquisitionDiagnostics()
    with pytest.raises(acquisition.AcquisitionError, match="^api-response-budget$"):
        acquisition.acquire_receipts(
            expected(), tmp_path / "oversized", github=OversizedClient(), diagnostics=diagnostics
        )
    assert not (tmp_path / "oversized").exists()
    report = diagnostics.snapshot()
    assert report["request_count"] == 1 and report["response_count"] == 0
    assert report["last_response_bytes"] is report["last_response_sha256"] is None


def test_diagnostic_counters_saturate_without_wrapping_or_changing_the_last_response():
    diagnostics = acquisition.AcquisitionDiagnostics()
    maximum = 2**53 - 1
    diagnostics._request_count = diagnostics._response_count = diagnostics._response_bytes = maximum
    diagnostics.request(acquisition.PREFIX)
    diagnostics.response(b"{}", 2)
    report = diagnostics.snapshot()
    assert (
        report["request_count"] == report["response_count"] == report["response_bytes"] == maximum
    )
    assert report["counters_saturated"] is True
    assert report["last_response_bytes"] == 2
    assert len(canonical(report)) < 4096


@pytest.mark.parametrize(
    "error,code",
    [
        (acquisition.AcquisitionError("github-request-timeout"), "github-request-timeout"),
        (acquisition.AcquisitionPending("selected-job-incomplete"), "selected-job-incomplete"),
        (ValueError("github-request-timeout"), "acquisition-failed"),
        (acquisition.AcquisitionError("private token\n\x1b[31m"), "acquisition-failed"),
        (acquisition.AcquisitionError("private" * 10000), "acquisition-failed"),
        (acquisition.AcquisitionError({"private": "token"}), "acquisition-failed"),
        (acquisition.AcquisitionError("github-request-timeout", "private"), "acquisition-failed"),
    ],
)
def test_diagnostic_codes_never_copy_unknown_exception_payloads(error, code):
    assert acquisition.diagnostic_code(error) == code


def test_diagnostic_codes_never_render_exceptions():
    class HostileError(acquisition.AcquisitionError):
        def __str__(self):
            raise AssertionError("Exception rendering must not run")

    assert acquisition.diagnostic_code(HostileError("private")) == "acquisition-failed"


def test_github_json_preserves_transport_failure_categories(monkeypatch):
    error = acquisition.AcquisitionError("github-request-timeout")

    def fail(*_args):
        raise error

    monkeypatch.setattr(acquisition.shutil, "which", lambda _: __file__)
    monkeypatch.setattr(acquisition.Github, "get", fail)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.Github().json(acquisition.PREFIX)
    assert caught.value is error


def test_github_json_still_rejects_invalid_json_with_fixed_category(monkeypatch):
    monkeypatch.setattr(acquisition.shutil, "which", lambda _: __file__)
    monkeypatch.setattr(acquisition.Github, "get", lambda *_args: b"private invalid JSON")
    with pytest.raises(acquisition.AcquisitionError, match="^invalid-github-json$"):
        acquisition.Github().json(acquisition.PREFIX)
