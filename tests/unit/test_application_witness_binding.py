"""Synthetic binding data only; no network, Ray, server or candidate execution."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import struct
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from qualification.witness_binding import bounds
from qualification.witness_binding import verify as checker

SOURCE, TREE, MERGE, BASE, MERGE_TREE = (letter * 40 for letter in "abcde")
REQUEST = checker.Request(checker.REPOSITORY_ID, 7, SOURCE, 10, 1, 11, 12)
PREFIX = bounds.PREFIX


def zipped(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            info = zipfile.ZipInfo(name)
            info.filename = info.orig_filename = name
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw)
    return output.getvalue()


class FakeGit:
    def __init__(self):
        self.raw = b"synthetic original Git tar bytes"
        self.files = {"src/django_ray/runtime/remote.py": b"# Read as bytes only\n"}
        self.objects = checker.profiles()["profiles"][0]["blobs"]
        self.requested_archives = []

    def tree(self, commit):
        return TREE if commit in {SOURCE, "f" * 40} else "0" * 40

    def entries(self, commit):
        assert commit == SOURCE
        return self.objects

    def source_archive(self, commit):
        self.requested_archives.append(commit)
        return self.raw, self.files


class FakeApi:
    def __init__(self, responses):
        self.responses, self.calls, self.hook = responses, [], None

    def get(self, endpoint, *, binary=False):
        bounds.route(endpoint)
        self.calls.append((endpoint, binary))
        if self.hook:
            self.hook(self, endpoint)
        result = self.responses[endpoint]
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)


@pytest.fixture
def candidate():
    git = FakeGit()
    files = {
        name: bounds.canonical({"product_semantics_not_verified": True}) for name in checker.FILES
    }
    profile = {"resource_profile": "standard", "validation_intent": "acceptance"}
    images = [
        {
            "RepoDigests": [f"localhost:5001/application-{role}@sha256:" + digit * 64],
            "RootFS": {"Layers": ["base"] + (["core"] if role == "core" else [])},
        }
        for role, digit in (("base", "1"), ("core", "2"))
    ]
    files.update(
        {
            "application-build/source.txt": (SOURCE + "\n" + TREE + "\n").encode(),
            "application-build/source.sha256": (
                hashlib.sha256(git.raw).hexdigest() + "  /tmp/original-source.tar\n"
            ).encode(),
            "application-build/resource-profile.txt": b"standard\n",
            "application-build/validation-intent.txt": b"acceptance\n",
            "application-build/images.json": bounds.canonical(images),
            "application-evidence/setup.json": bounds.canonical(
                {"archives": {"source": checker.runtime_archive(git.files)}}
            ),
            "application-evidence/summary.json": bounds.canonical(
                {
                    "status": "passed",
                    "namespace_removed": True,
                    "source_tree": TREE,
                    "image": images[1]["RepoDigests"][0],
                    "cold_ray": "required",
                    "complete_application_gate": False,
                    "qualification_profile": checker.STANDARD,
                }
            ),
        }
    )
    receipt_zip = zipped(files)
    manifest = [
        {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in sorted(files.items())
    ]
    github = {
        "repository": bounds.REPOSITORY,
        "repository_id": checker.REPOSITORY_ID,
        "run_id": 10,
        "attempt": 1,
        "actor_id": checker.OWNER_ID,
        "event": "pull_request",
        "event_sha": MERGE,
        "workflow_sha": MERGE,
        "workflow_path": checker.WORKFLOW,
        "source_head": SOURCE,
        "pull_request": {
            "number": 7,
            "author_id": checker.OWNER_ID,
            "base_repository_id": checker.REPOSITORY_ID,
            "head_repository_id": checker.REPOSITORY_ID,
            "base_sha": BASE,
            "head_sha": SOURCE,
        },
    }
    record = {
        "schema": "django-ray.application-execution-witness",
        "schema_version": 1,
        "mode": "direct_execution",
        "status": "passed",
        "failures": [],
        "github": github,
        "profile": profile,
        "runner": {
            "os": "Linux",
            "arch": "X64",
            "environment": "github-hosted",
            "ImageOS": None,
            "ImageVersion": None,
        },
        "steps": dict.fromkeys(checker.STEP_NAMES, "success"),
        "source": {
            "head": SOURCE,
            "tree": TREE,
            "archive_sha256": hashlib.sha256(git.raw).hexdigest(),
        },
        "tested_images": {
            "base": images[0]["RepoDigests"][0],
            "application": images[1]["RepoDigests"][0],
        },
        "observation": {
            "status": "passed",
            "namespace_removed": True,
            "cold_ray": "required",
            "source_tree_matched": True,
            "qualification_profile_sha256": hashlib.sha256(
                bounds.canonical(checker.STANDARD)
            ).hexdigest(),
        },
        "receipt_artifact": {
            "id": 11,
            "name": "application-core-10",
            "digest": bounds.digest(receipt_zip),
        },
        "receipt_files": manifest,
        "self_authenticating": False,
        "reuse_enabled": False,
        "complete_application_gate": False,
    }
    witness_zip = zipped({"execution-witness.json": bounds.canonical(record)})
    repository = {
        "id": checker.REPOSITORY_ID,
        "full_name": bounds.REPOSITORY,
        "owner": {"id": checker.OWNER_ID},
    }
    run = {
        "id": 10,
        "run_attempt": 1,
        "head_sha": SOURCE,
        "workflow_id": 14,
        "path": checker.WORKFLOW,
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "repository": repository,
        "head_repository": repository,
        "actor": {"id": checker.OWNER_ID},
        "triggering_actor": {"id": checker.OWNER_ID},
        "pull_requests": [{"number": 7}],
        "referenced_workflows": [
            {
                "path": f"{bounds.REPOSITORY}/{checker.CALLED}@{MERGE}",
                "ref": "refs/pull/7/merge",
                "sha": MERGE,
            }
        ],
        "created_at": "2026-11-16T00:00:00Z",
        "updated_at": "2026-11-16T00:00:20Z",
        "run_started_at": "2026-11-16T00:00:01Z",
    }
    steps = [
        {
            "name": name,
            "number": index,
            "status": "completed",
            "conclusion": "success",
            "started_at": f"2026-11-16T00:00:{index:02d}Z",
            "completed_at": f"2026-11-16T00:00:{index + 1:02d}Z",
        }
        for index, name in enumerate(
            [*checker.STEP_NAMES.values(), checker.PRODUCER, checker.UPLOAD], 1
        )
    ]
    job = {
        "id": 13,
        "name": "application / Application Core (Linux)",
        "run_id": 10,
        "run_attempt": 1,
        "head_sha": SOURCE,
        "status": "completed",
        "conclusion": "success",
        "labels": ["ubuntu-latest"],
        "steps": steps,
    }
    artifacts = []
    for oid, name, raw, created in (
        (11, "application-core-10", receipt_zip, "09"),
        (12, "application-execution-witness-10-1", witness_zip, "11"),
    ):
        artifacts.append(
            {
                "id": oid,
                "name": name,
                "expired": False,
                "size_in_bytes": len(raw),
                "digest": bounds.digest(raw),
                "created_at": f"2026-11-16T00:00:{created}.500Z",
                "workflow_run": {
                    "id": 10,
                    "repository_id": checker.REPOSITORY_ID,
                    "head_repository_id": checker.REPOSITORY_ID,
                    "head_sha": SOURCE,
                },
            }
        )
    responses = {
        PREFIX: repository,
        PREFIX + "/pulls/7": {
            "number": 7,
            "user": {"id": checker.OWNER_ID},
            "state": "closed",
            "head": {"sha": SOURCE, "repo": repository},
            "base": {"sha": BASE, "repo": repository},
        },
        PREFIX + "/actions/workflows/qualification-gate.yml": {"path": checker.WORKFLOW, "id": 14},
        PREFIX + "/actions/runs/10": copy.deepcopy(run),
        PREFIX + "/actions/runs/10/attempts/1": copy.deepcopy(run),
        PREFIX + "/actions/runs/10/attempts/1/jobs?per_page=64": {
            "total_count": 1,
            "jobs": [copy.deepcopy(job)],
        },
        PREFIX + "/actions/jobs/13": copy.deepcopy(job),
        PREFIX + "/actions/artifacts/11": artifacts[0],
        PREFIX + "/actions/artifacts/12": artifacts[1],
        PREFIX + "/actions/artifacts/11/zip": receipt_zip,
        PREFIX + "/actions/artifacts/12/zip": witness_zip,
        PREFIX
        + f"/actions/workflows/qualification-gate.yml/runs?head_sha={SOURCE}&per_page=32&page=1": {
            "total_count": 1,
            "workflow_runs": [run],
        },
        PREFIX + f"/git/commits/{SOURCE}": {"sha": SOURCE, "tree": {"sha": TREE}},
        PREFIX + f"/git/commits/{MERGE}": {
            "sha": MERGE,
            "tree": {"sha": MERGE_TREE},
            "parents": [{"sha": BASE}, {"sha": SOURCE}],
        },
        PREFIX + f"/git/trees/{MERGE_TREE}?recursive=1": {
            "sha": MERGE_TREE,
            "truncated": False,
            "tree": [
                {"path": name, "sha": git.objects[name], "type": "blob"}
                for name in checker.profiles()["caller_paths"]
            ],
        },
    }
    return FakeApi(responses), git, record, files


def rewrite_witness(api, record):
    raw = zipped({"execution-witness.json": bounds.canonical(record)})
    api.responses[PREFIX + "/actions/artifacts/12/zip"] = raw
    meta = api.responses[PREFIX + "/actions/artifacts/12"]
    meta.update(size_in_bytes=len(raw), digest=bounds.digest(raw))


def assert_not_qualified(result):
    assert result["semantic_validation"] == "not_performed"
    assert all(
        result[name] is False for name in checker.BOUNDARIES if name != "semantic_validation"
    )
    assert checker.exit_status(result) != 0


def test_binding_is_useful_but_not_a_semantic_or_authorization_success(candidate):
    api, git, _, _ = candidate
    result = checker.verify(REQUEST, api, git)
    assert result["metadata_binding_verified"] is True, result
    assert result["file_count"] == 25 and result["step_count"] == 9
    assert result["product_receipt_files_present"] == 18
    assert git.requested_archives == [SOURCE]
    assert len(api.calls) <= 48
    assert checker.exit_status(result) == 3
    assert_not_qualified(result)


def test_tracked_definition_digest_accepts_crlf_checkout_without_policy_changes(
    tmp_path, monkeypatch
):
    raw = Path(checker.__file__).with_name("profiles.json").read_bytes()
    expected = checker.profiles()
    (tmp_path / "profiles.json").write_bytes(raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    monkeypatch.setattr(checker, "__file__", str(tmp_path / "trusted.py"))
    assert checker.profiles() == expected


def test_comparison_head_does_not_relabel_archive_or_execution(candidate):
    api, git, _, _ = candidate
    result = checker.verify(replace(REQUEST, comparison_source="f" * 40), api, git)
    assert result["metadata_binding_verified"] is True
    assert result["comparison"] == {
        "source": "f" * 40,
        "tree_equal": True,
        "execution_claim": False,
    }
    assert git.requested_archives == [SOURCE]
    assert result["original_source"] == SOURCE
    assert_not_qualified(result)


def test_empty_run_association_needs_actual_caller_ref_and_unchanged_source_head(candidate):
    api, git, _, _ = candidate
    for endpoint in ("/actions/runs/10", "/actions/runs/10/attempts/1"):
        api.responses[PREFIX + endpoint]["pull_requests"] = []
    result = checker.verify(REQUEST, api, git)
    assert result["metadata_binding_verified"] is True
    assert result["api_pull_request_association"] == "caller_ref_and_current_source_head"
    assert_not_qualified(result)


@pytest.mark.parametrize("change", ["head", "caller", "wrong_ref", "contradiction"])
def test_empty_or_contradictory_run_association_cannot_be_filled_from_witness(candidate, change):
    api, git, _, _ = candidate
    for endpoint in ("/actions/runs/10", "/actions/runs/10/attempts/1"):
        run = api.responses[PREFIX + endpoint]
        run["pull_requests"] = [] if change != "contradiction" else [{"number": 8}]
        if change == "caller":
            run["referenced_workflows"] = []
        if change == "wrong_ref":
            run["referenced_workflows"][0]["ref"] = "refs/pull/8/merge"
    if change == "head":
        api.responses[PREFIX + "/pulls/7"]["head"]["sha"] = "f" * 40
    result = checker.verify(REQUEST, api, git)
    assert result["metadata_binding_verified"] is False
    assert_not_qualified(result)


@pytest.mark.parametrize(
    "key,value",
    [
        ("attempt", 2),
        ("attempt", True),
        ("run", 0),
        ("repository_id", 1),
        ("source", "https://secret.invalid/?token=private"),
        ("run", 2**64),
    ],
)
def test_invalid_request_never_acquires_any_data(candidate, key, value):
    api, git, _, _ = candidate
    result = checker.verify(replace(REQUEST, **{key: value}), api, git)
    assert not result["metadata_binding_verified"] and api.calls == []
    assert "private" not in json.dumps(result)
    assert_not_qualified(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "incomplete"),
        ("mode", "reuse"),
        ("self_authenticating", True),
        ("reuse_enabled", True),
        ("complete_application_gate", True),
        ("failures", ["source_unavailable"]),
        ("schema_version", True),
    ],
)
def test_partial_or_derived_record_never_binds(candidate, field, value):
    api, git, record, _ = candidate
    record[field] = value
    rewrite_witness(api, record)
    result = checker.verify(REQUEST, api, git)
    assert not result["metadata_binding_verified"]
    assert_not_qualified(result)


@pytest.mark.parametrize(
    "change",
    [
        "fork",
        "author",
        "actor",
        "rerun",
        "wrong_run",
        "wrong_head",
        "wrong_job",
        "step_failed",
        "step_skipped",
        "duplicate_step",
        "expired",
        "replaced",
        "upload_time",
        "digest",
        "wrong_artifact_run",
        "truncated",
        "merge_parent",
        "caller_definition",
        "source_definition",
        "missing_object",
    ],
)
def test_single_fact_mutations_refuse_binding(candidate, change):
    api, git, _, _ = candidate
    responses = api.responses
    run = responses[PREFIX + "/actions/runs/10"]
    job = responses[PREFIX + "/actions/runs/10/attempts/1/jobs?per_page=64"]["jobs"][0]
    artifact = responses[PREFIX + "/actions/artifacts/11"]
    if change == "fork":
        responses[PREFIX + "/pulls/7"]["head"]["repo"] = {"id": 1, "full_name": "external/fork"}
    elif change == "author":
        responses[PREFIX + "/pulls/7"]["user"]["id"] = 1
    elif change == "actor":
        run["triggering_actor"]["id"] = 1
    elif change == "rerun":
        run["run_attempt"] = 2
    elif change == "wrong_run":
        run["id"] = 15
    elif change == "wrong_head":
        run["head_sha"] = BASE
    elif change == "wrong_job":
        job["run_id"] = 15
    elif change in {"step_failed", "step_skipped"}:
        job["steps"][4]["conclusion"] = "failure" if change == "step_failed" else "skipped"
    elif change == "duplicate_step":
        job["steps"].append(job["steps"][0])
    elif change == "expired":
        artifact["expired"] = True
    elif change == "replaced":
        artifact["id"] = 99
    elif change == "upload_time":
        artifact["created_at"] = "2026-11-16T00:00:01Z"
    elif change == "digest":
        artifact["digest"] = "sha256:" + "0" * 64
    elif change == "wrong_artifact_run":
        artifact["workflow_run"]["id"] = 15
    elif change == "truncated":
        responses[
            PREFIX
            + f"/actions/workflows/qualification-gate.yml/runs?head_sha={SOURCE}&per_page=32&page=1"
        ]["total_count"] = 33
    elif change == "merge_parent":
        responses[PREFIX + f"/git/commits/{MERGE}"]["parents"].reverse()
    elif change == "caller_definition":
        responses[PREFIX + f"/git/trees/{MERGE_TREE}?recursive=1"]["tree"][0]["sha"] = "0" * 40
    elif change == "source_definition":
        git.objects = {**git.objects, "qualification/application/new_unreviewed.py": "0" * 40}
    elif change == "missing_object":

        def missing(_commit):
            raise bounds.BindingError("local_git_unavailable", unavailable=True)

        git.tree = missing
    result = checker.verify(REQUEST, api, git)
    assert not result["metadata_binding_verified"], (change, result)
    assert_not_qualified(result)


@pytest.mark.parametrize(
    "status,conclusion",
    [
        ("in_progress", None),
        ("completed", "failure"),
        ("completed", "cancelled"),
        ("completed", "skipped"),
    ],
)
def test_later_exact_head_failure_or_pending_vetoes_older_success(candidate, status, conclusion):
    api, git, _, _ = candidate
    history = api.responses[
        PREFIX
        + f"/actions/workflows/qualification-gate.yml/runs?head_sha={SOURCE}&per_page=32&page=1"
    ]
    later = {
        **copy.deepcopy(history["workflow_runs"][0]),
        "id": 99,
        "created_at": "2026-11-16T00:01:00Z",
        "status": status,
        "conclusion": conclusion,
    }
    history["workflow_runs"].append(later)
    history["total_count"] = 2
    result = checker.verify(REQUEST, api, git)
    assert result["failure_codes"] == ["latest_relevant_run_not_successful"]


def test_context_is_reread_after_bytes_and_changes_are_refused(candidate):
    api, git, _, _ = candidate

    def change(api, endpoint):
        if endpoint == PREFIX + "/actions/artifacts/12/zip":
            api.responses[PREFIX + "/pulls/7"]["base"]["sha"] = "f" * 40

    api.hook = change
    result = checker.verify(REQUEST, api, git)
    assert result["failure_codes"] == ["context_changed"]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":1e9999}',
        b'{"a":9999999999999999999999}',
        b"[" * 40 + b"0" + b"]" * 40,
        b'"' + b"x" * 65537 + b'"',
    ],
)
def test_strict_json_refuses_aliases_nonfinite_overflow_depth_and_text(raw):
    with pytest.raises(bounds.BindingError, match="json_invalid"):
        bounds.decode(raw)


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "a\\b", "C:/escape", "a/./b", "a//b", "a./b", "a /b"]
)
def test_zip_paths_are_portable_and_never_extracted(name):
    with pytest.raises(bounds.BindingError, match="archive_structure_invalid"):
        bounds.archive(zipped({name: b"x"}), (name,))


def test_zip_duplicate_case_alias_and_overlapping_members_refused():
    with pytest.raises(bounds.BindingError, match="archive_structure_invalid"):
        bounds.archive(zipped({"one": b"1", "ONE": b"2"}), ("one",))
    raw = bytearray(zipped({"one": b"1", "two": b"2"}))
    second = raw.find(b"PK\x01\x02", raw.find(b"PK\x01\x02") + 1)
    struct.pack_into("<I", raw, second + 42, 0)
    with pytest.raises(bounds.BindingError, match="archive_structure_invalid"):
        bounds.archive(bytes(raw), ("one", "two"))


def test_selected_zip_total_is_refused_before_any_member_opens(monkeypatch):
    files = {str(index): b"x" * (256 * 1024) for index in range(9)}
    raw = zipped(files)
    opened = Mock(side_effect=AssertionError("Selected ZIP expansion exceeded its preflight cap"))
    monkeypatch.setattr(zipfile.ZipFile, "open", opened)
    with pytest.raises(bounds.BindingError, match="archive_budget_exceeded"):
        bounds.archive(raw, tuple(files))
    opened.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.invalid/",
        PREFIX + "/../actions",
        PREFIX + "/actions/runs/1?token=secret",
        PREFIX + "/actions/jobs/0",
        PREFIX + "/actions/runs/1/attempts/2",
        PREFIX + "/actions/artifacts/1/zip?x=secret",
    ],
)
def test_route_injection_never_reaches_transport(value):
    with pytest.raises(bounds.BindingError, match="transport_route_invalid"):
        bounds.route(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://productionresultssa1.blob.core.windows.net/x",
        "https://evil.invalid/x",
        "https://token@productionresultssa1.blob.core.windows.net/x",
        "https://127.0.0.1/x",
        "https://productionresultssa1.blob.core.windows.net:444/x",
    ],
)
def test_redirect_origins_and_credentials_are_restricted(value):
    with pytest.raises(bounds.BindingError, match="redirect_target_invalid"):
        bounds.redirect(value)
