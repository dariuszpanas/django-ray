"""Metadata/artifact binding only. Never product qualification or authorization."""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .bounds import (
    MAX_OUTPUT,
    MIB,
    PREFIX,
    REPOSITORY,
    BindingError,
    Budget,
    archive,
    canonical,
    decode,
    digest,
    identity,
    process,
    require,
    sha,
    timestamp,
)
from .git_data import Git, host_program
from .http_transport import Github

REPOSITORY_ID = 790856348
OWNER_ID = 15094983
WORKFLOW = ".github/workflows/qualification-gate.yml"
CALLED = ".github/workflows/application-qualification.yml"

PROFILE_DIGEST = "57ca6ebf4c57db73ae01b2eb7eb661ca14249bb18619777d6494c6984ad3531b"
STEP_NAMES = {
    "checkout": "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "profile": "Validate the fixed resource profile before allocating capacity",
    "ci": "Require the current source Linux CI checkpoint",
    "tools": "Install pinned public test tools",
    "build": "Build the committed candidate with a disposable local registry",
    "cluster": "Create a bounded disposable Kind cluster",
    "native": "Run the native application core test",
    "cleanup": "Remove only this job's disposable infrastructure",
    "receipts": "Upload application receipts and build identity",
}
PRODUCER = "Record bounded direct execution facts"
UPLOAD = "Retain the attempt-qualified execution witness"
FILES = tuple(
    sorted(
        [
            f"application-evidence/{name}.json"
            for name in (
                "setup",
                "reporting",
                "producers",
                "summary",
                *(
                    f"{generation}-{layer}"
                    for generation in ("before", "after")
                    for layer in (
                        "first",
                        "nodes",
                        "core",
                        "workflows",
                        "rq2",
                        "showcase",
                        "protocol",
                        "protected-logs",
                    )
                ),
            )
        ]
        + [
            f"application-build/{name}"
            for name in (
                "source.txt",
                "source.sha256",
                "images.json",
                "validation-intent.txt",
                "resource-profile.txt",
            )
        ]
    )
)
STANDARD = {
    "resource_profile": "standard",
    "validation_intent": "acceptance",
    "requested_ray_resources": {
        name: {"logical_cpus": 1, "cpu_request": "750m", "cpu_limit": "750m"}
        for name in ("head", "worker")
    },
}
BOUNDARIES = {
    "semantic_validation": "not_performed",
    "qualification_admission": False,
    "reuse_enabled": False,
    "complete_application_gate": False,
    "self_authenticating": False,
    "current_head_execution": False,
    "authorizes_workflow_review_or_merge": False,
}


@dataclass(frozen=True)
class Request:
    repository_id: int
    pr: int
    source: str
    run: int
    attempt: int
    receipt_artifact: int
    witness_artifact: int
    comparison_source: str | None = None

    def validate(self):
        for value in (
            self.repository_id,
            self.pr,
            self.run,
            self.attempt,
            self.receipt_artifact,
            self.witness_artifact,
        ):
            identity(value)
        require(self.repository_id == REPOSITORY_ID, "identity_mismatch")
        require(self.attempt == 1, "attempt_unsupported")
        require(self.receipt_artifact != self.witness_artifact, "request_invalid")
        sha(self.source)
        if self.comparison_source is not None:
            sha(self.comparison_source)


def profiles():
    with Path(__file__).with_name("profiles.json").open("rb") as source:
        raw = source.read(128 * 1024 + 1)
    require(0 < len(raw) <= 128 * 1024, "workflow_definition_unsupported")
    raw = raw.replace(b"\r\n", b"\n")  # Match the tracked LF blob after Windows checkout.
    require(
        len(raw) <= 128 * 1024 and hashlib.sha256(raw).hexdigest() == PROFILE_DIGEST,
        "workflow_definition_unsupported",
    )
    return decode(raw)


def exact(actual, expected):
    return canonical(actual) == canonical(expected)


def known_definitions(entries, caller_entries):
    policy = profiles()
    for candidate in policy["profiles"]:
        expected = candidate["blobs"]
        actual = {
            name: value
            for name, value in entries.items()
            if name in expected
            or any(name.startswith(prefix) for prefix in policy["inventory_prefixes"])
        }
        if actual == expected and all(
            caller_entries.get(name) == expected[name] for name in policy["caller_paths"]
        ):
            return candidate["id"]
    raise BindingError("workflow_definition_unsupported", unavailable=True)


def pull_snapshot(value, request):
    for oid in (
        value["number"],
        value["user"]["id"],
        value["base"]["repo"]["id"],
        value["head"]["repo"]["id"],
    ):
        identity(oid)
    require(value["state"] in {"open", "closed"}, "metadata_invalid")
    require(value["number"] == request.pr and type(value["number"]) is int, "identity_mismatch")
    require(
        value["user"]["id"] == OWNER_ID and type(value["user"]["id"]) is int, "identity_mismatch"
    )
    require(
        value["base"]["repo"]["id"] == value["head"]["repo"]["id"] == REPOSITORY_ID,
        "identity_mismatch",
    )
    require(
        value["base"]["repo"]["full_name"] == value["head"]["repo"]["full_name"] == REPOSITORY,
        "identity_mismatch",
    )
    return {
        "head": sha(value["head"]["sha"]),
        "base": sha(value["base"]["sha"]),
        "state": value["state"],
        "author": OWNER_ID,
    }


def run_snapshot(value, request, workflow_id, *, current_source_head=False):
    for oid in (
        value["workflow_id"],
        value["repository"]["id"],
        value["head_repository"]["id"],
        value["repository"]["owner"]["id"],
        value["actor"]["id"],
        value["triggering_actor"]["id"],
    ):
        identity(oid)
    require(value["id"] == request.run and type(value["id"]) is int, "identity_mismatch")
    require(value["run_attempt"] == 1 and type(value["run_attempt"]) is int, "attempt_unsupported")
    require(
        value["head_sha"] == request.source and value["workflow_id"] == workflow_id,
        "identity_mismatch",
    )
    require(
        value["path"] == WORKFLOW and value["event"] == "pull_request",
        "qualification_profile_unsupported",
    )
    require(
        value["status"] == "completed" and value["conclusion"] == "success",
        "direct_record_incomplete",
    )
    require(
        value["repository"]["id"] == value["head_repository"]["id"] == REPOSITORY_ID
        and value["repository"]["owner"]["id"] == OWNER_ID,
        "identity_mismatch",
    )
    require(
        value["actor"]["id"] == value["triggering_actor"]["id"] == OWNER_ID, "identity_mismatch"
    )
    associations = value["pull_requests"]
    require(type(associations) is list and len(associations) <= 1, "identity_mismatch")
    if associations:
        require(identity(associations[0]["number"]) == request.pr, "identity_mismatch")
        association = "run_pull_request_and_caller_ref"
    else:
        require(current_source_head, "original_merge_context_unavailable", unavailable=True)
        association = "caller_ref_and_current_source_head"
    referenced = [
        item
        for item in value["referenced_workflows"]
        if item["path"].startswith(REPOSITORY + "/" + CALLED + "@")
    ]
    require(
        len(value["referenced_workflows"]) <= 16 and len(referenced) == 1,
        "original_merge_context_unavailable",
        unavailable=True,
    )
    called = referenced[0]
    merge = sha(called["sha"])
    require(
        called["ref"] == f"refs/pull/{request.pr}/merge"
        and called["path"] == f"{REPOSITORY}/{CALLED}@{merge}",
        "identity_mismatch",
    )
    return {
        "id": request.run,
        "attempt": 1,
        "source": request.source,
        "merge": merge,
        "created": value["created_at"],
        "updated": value["updated_at"],
        "run_started": value["run_started_at"],
        "association": association,
    }


def job_snapshot(value, request):
    for key in ("id", "run_id", "run_attempt"):
        identity(value[key])
    require(
        value["name"] == "application / Application Core (Linux)"
        and value["run_id"] == request.run
        and value["run_attempt"] == 1
        and value["head_sha"] == request.source,
        "identity_mismatch",
    )
    require(
        value["status"] == "completed"
        and value["conclusion"] == "success"
        and value["labels"] == ["ubuntu-latest"],
        "direct_record_incomplete",
    )
    identity(value["id"])
    require(
        type(value["steps"]) is list and 11 <= len(value["steps"]) <= 32, "direct_steps_mismatch"
    )
    steps = {step["name"]: step for step in value["steps"]}
    require(len(steps) == len(value["steps"]), "direct_steps_mismatch")
    required = [*STEP_NAMES.values(), PRODUCER, UPLOAD]
    numbers = []
    for name in required:
        step = steps[name]
        require(
            step["status"] == "completed" and step["conclusion"] == "success",
            "direct_steps_mismatch",
        )
        numbers.append(identity(step["number"]))
        require(
            timestamp(step["started_at"]) <= timestamp(step["completed_at"]),
            "direct_steps_mismatch",
        )
    require(numbers == sorted(set(numbers)), "direct_steps_mismatch")
    return {
        "id": value["id"],
        "steps": {
            name: {key: steps[name][key] for key in ("number", "started_at", "completed_at")}
            for name in required
        },
    }


def artifact_snapshot(value, request, artifact_id, steps, witness):
    expected = (
        f"application-execution-witness-{request.run}-1"
        if witness
        else f"application-core-{request.run}"
    )
    require(
        value["id"] == artifact_id
        and type(value["id"]) is int
        and value["name"] == expected
        and value["expired"] is False,
        "artifact_provenance_mismatch",
    )
    workflow = value["workflow_run"]
    for key in ("id", "repository_id", "head_repository_id"):
        identity(workflow[key])
    require(
        workflow["id"] == request.run
        and workflow["repository_id"] == workflow["head_repository_id"] == REPOSITORY_ID
        and workflow["head_sha"] == request.source,
        "artifact_provenance_mismatch",
    )
    require(
        type(value["size_in_bytes"]) is int
        and 0 < value["size_in_bytes"] <= MIB
        and re.fullmatch("sha256:[a-f0-9]{64}", value["digest"]),
        "artifact_integrity_mismatch",
    )
    upload = steps[UPLOAD if witness else STEP_NAMES["receipts"]]
    created = timestamp(value["created_at"])
    require(
        timestamp(upload["started_at"])
        <= created
        < timestamp(upload["completed_at"]) + timedelta(seconds=1),
        "artifact_provenance_mismatch",
    )
    return {
        key: value[key]
        for key in ("id", "name", "digest", "size_in_bytes", "created_at", "expired")
    }


def history_snapshot(value, head, selected=None):
    require(
        type(value["total_count"]) is int
        and 0 <= value["total_count"] <= 32
        and value["total_count"] == len(value["workflow_runs"]),
        "history_incomplete",
        unavailable=True,
    )
    runs = []
    for item in value["workflow_runs"]:
        require(item["head_sha"] == head and item["path"] == WORKFLOW, "identity_mismatch")
        runs.append(
            {
                key: item[key]
                for key in (
                    "id",
                    "head_sha",
                    "path",
                    "created_at",
                    "run_attempt",
                    "status",
                    "conclusion",
                )
            }
        )
        identity(item["id"])
        identity(item["run_attempt"])
        timestamp(item["created_at"])
    require(len({item["id"] for item in runs}) == len(runs), "identity_mismatch")
    runs.sort(key=lambda item: (timestamp(item["created_at"]), item["id"]))
    if selected is not None:
        require(
            any(item["id"] == selected for item in runs), "history_incomplete", unavailable=True
        )
        cutoff = next(index for index, item in enumerate(runs) if item["id"] == selected)
        relevant = runs[cutoff:]
    else:
        relevant = runs[-1:]
    require(
        all(
            item["status"] == "completed"
            and item["conclusion"] == "success"
            and item["run_attempt"] == 1
            for item in relevant
        ),
        "latest_relevant_run_not_successful",
    )
    return runs


def metadata(api, request):
    repo = api.get(PREFIX)
    require(
        repo["id"] == REPOSITORY_ID
        and repo["full_name"] == REPOSITORY
        and repo["owner"]["id"] == OWNER_ID,
        "identity_mismatch",
    )
    pr = pull_snapshot(api.get(f"{PREFIX}/pulls/{request.pr}"), request)
    workflow = api.get(PREFIX + "/actions/workflows/qualification-gate.yml")
    require(workflow["path"] == WORKFLOW, "identity_mismatch")
    workflow_id = identity(workflow["id"])
    current = run_snapshot(
        api.get(f"{PREFIX}/actions/runs/{request.run}"),
        request,
        workflow_id,
        current_source_head=pr["head"] == request.source,
    )
    attempt = run_snapshot(
        api.get(f"{PREFIX}/actions/runs/{request.run}/attempts/1"),
        request,
        workflow_id,
        current_source_head=pr["head"] == request.source,
    )
    require(current == attempt, "context_changed")
    jobs = api.get(f"{PREFIX}/actions/runs/{request.run}/attempts/1/jobs?per_page=64")
    require(
        type(jobs["total_count"]) is int
        and jobs["total_count"] == len(jobs["jobs"]) <= 64
        and len({j["id"] for j in jobs["jobs"]}) == len(jobs["jobs"]),
        "history_incomplete",
        unavailable=True,
    )
    selected = [
        job for job in jobs["jobs"] if job["name"] == "application / Application Core (Linux)"
    ]
    require(len(selected) == 1, "identity_mismatch")
    job = job_snapshot(selected[0], request)
    detail = job_snapshot(api.get(f"{PREFIX}/actions/jobs/{job['id']}"), request)
    require(detail == job, "context_changed")
    receipt = artifact_snapshot(
        api.get(f"{PREFIX}/actions/artifacts/{request.receipt_artifact}"),
        request,
        request.receipt_artifact,
        job["steps"],
        False,
    )
    witness = artifact_snapshot(
        api.get(f"{PREFIX}/actions/artifacts/{request.witness_artifact}"),
        request,
        request.witness_artifact,
        job["steps"],
        True,
    )
    require(
        timestamp(receipt["created_at"]) <= timestamp(witness["created_at"]),
        "artifact_provenance_mismatch",
    )
    histories = {}
    for head in dict.fromkeys((request.source, pr["head"])):
        histories[head] = history_snapshot(
            api.get(
                f"{PREFIX}/actions/workflows/qualification-gate.yml/runs?head_sha={head}&per_page=32&page=1"
            ),
            head,
            request.run if head == request.source else None,
        )
    return {
        "pr": pr,
        "run": current,
        "job": job,
        "receipt": receipt,
        "witness": witness,
        "histories": histories,
    }


def runtime_archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for name, raw in sorted(files.items()):
            if (
                name.endswith(".py")
                and name.split("/")[0] in {"src", "testproject"}
                and "__pycache__" not in name.split("/")
            ):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system, info.compress_type, info.external_attr = (
                    3,
                    zipfile.ZIP_DEFLATED,
                    0o100644 << 16,
                )
                target.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    raw = output.getvalue()
    require(len(raw) <= 32 * MIB, "source_archive_invalid")
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def bind_record(raw, files, request, observed, git, api):
    record = decode(raw, 32 * 1024)
    require(canonical(record) == raw, "witness_schema_invalid")
    require(
        set(record)
        == {
            "schema",
            "schema_version",
            "mode",
            "status",
            "failures",
            "github",
            "profile",
            "runner",
            "steps",
            "source",
            "tested_images",
            "observation",
            "receipt_artifact",
            "receipt_files",
            "self_authenticating",
            "reuse_enabled",
            "complete_application_gate",
        },
        "witness_schema_invalid",
    )
    require(
        record["schema"] == "django-ray.application-execution-witness"
        and type(record["schema_version"]) is int
        and record["schema_version"] == 1,
        "witness_schema_invalid",
    )
    require(
        record["mode"] == "direct_execution"
        and record["status"] == "passed"
        and record["failures"] == []
        and all(
            record[key] is False
            for key in ("self_authenticating", "reuse_enabled", "complete_application_gate")
        ),
        "direct_record_incomplete",
    )
    require(record["steps"] == dict.fromkeys(STEP_NAMES, "success"), "direct_steps_mismatch")
    require(
        record["profile"] == {"resource_profile": "standard", "validation_intent": "acceptance"},
        "qualification_profile_unsupported",
    )
    merge = observed["run"]["merge"]
    commit = api.get(f"{PREFIX}/git/commits/{merge}")
    require(
        commit["sha"] == merge
        and len(commit["parents"]) == 2
        and commit["parents"][1]["sha"] == request.source,
        "merge_parent_mismatch",
    )
    base, merge_tree = sha(commit["parents"][0]["sha"]), sha(commit["tree"]["sha"])
    source_tree = git.tree(request.source)
    original = api.get(f"{PREFIX}/git/commits/{request.source}")
    require(
        original["sha"] == request.source and original["tree"]["sha"] == source_tree,
        "original_source_mismatch",
    )
    tree = api.get(f"{PREFIX}/git/trees/{merge_tree}?recursive=1")
    require(
        tree["sha"] == merge_tree and tree["truncated"] is False and len(tree["tree"]) <= 4096,
        "workflow_definition_unsupported",
        unavailable=True,
    )
    caller_entries = {item["path"]: item["sha"] for item in tree["tree"] if item["type"] == "blob"}
    require(
        len(caller_entries) == sum(item["type"] == "blob" for item in tree["tree"]),
        "workflow_definition_unsupported",
    )
    profile = known_definitions(git.entries(request.source), caller_entries)
    github = {
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "run_id": request.run,
        "attempt": 1,
        "actor_id": OWNER_ID,
        "event": "pull_request",
        "event_sha": merge,
        "workflow_sha": merge,
        "workflow_path": WORKFLOW,
        "source_head": request.source,
        "pull_request": {
            "number": request.pr,
            "author_id": OWNER_ID,
            "base_repository_id": REPOSITORY_ID,
            "head_repository_id": REPOSITORY_ID,
            "base_sha": base,
            "head_sha": request.source,
        },
    }
    require(exact(record["github"], github), "identity_mismatch")
    manifest = [
        {"path": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for name, payload in sorted(files.items())
    ]
    require(
        exact(record["receipt_files"], manifest) and len(manifest) == 25,
        "receipt_manifest_mismatch",
    )
    source_lines = files["application-build/source.txt"].decode("ascii").splitlines()
    require(source_lines == [request.source, source_tree], "original_source_mismatch")
    archive_line = files["application-build/source.sha256"].decode("ascii").strip()
    require(re.fullmatch(r"[a-f0-9]{64}  [^\r\n]{1,512}", archive_line), "original_source_mismatch")
    tar, source_files = git.source_archive(request.source)
    archive_sha = hashlib.sha256(tar).hexdigest()
    require(archive_line[:64] == archive_sha, "original_source_mismatch")
    exported = {"head": request.source, "tree": source_tree, "archive_sha256": archive_sha}
    require(record["source"] == exported, "original_source_mismatch")
    for name, expected in (
        ("resource-profile.txt", "standard"),
        ("validation-intent.txt", "acceptance"),
    ):
        require(
            files["application-build/" + name].decode("ascii").strip() == expected,
            "qualification_profile_unsupported",
        )
    images = decode(files["application-build/images.json"], 256 * 1024)
    require(type(images) is list and len(images) == 2, "witness_schema_invalid")
    references = []
    for image, role in zip(images, ("base", "core"), strict=True):
        refs = image["RepoDigests"]
        require(
            type(refs) is list
            and len(refs) == 1
            and re.fullmatch(rf"localhost:5001/application-{role}@sha256:[a-f0-9]{{64}}", refs[0]),
            "witness_schema_invalid",
        )
        references.append(refs[0])
    require(
        record["tested_images"] == {"base": references[0], "application": references[1]},
        "witness_schema_invalid",
    )
    layers = images[0]["RootFS"]["Layers"]
    require(
        type(layers) is list and layers and images[1]["RootFS"]["Layers"][: len(layers)] == layers,
        "witness_schema_invalid",
    )
    summary = decode(files["application-evidence/summary.json"], 256 * 1024)
    require(
        summary["status"] == "passed"
        and summary["namespace_removed"] is True
        and summary["source_tree"] == source_tree
        and summary["image"] == references[1]
        and summary["cold_ray"] == "required"
        and summary["complete_application_gate"] is False
        and exact(summary["qualification_profile"], STANDARD),
        "direct_record_incomplete",
    )
    require(
        record["observation"]
        == {
            "status": "passed",
            "namespace_removed": True,
            "cold_ray": "required",
            "source_tree_matched": True,
            "qualification_profile_sha256": hashlib.sha256(canonical(STANDARD)).hexdigest(),
        },
        "witness_schema_invalid",
    )
    require(
        record["receipt_artifact"]
        == {key: observed["receipt"][key] for key in ("id", "digest", "name")},
        "artifact_provenance_mismatch",
    )
    identity(record["receipt_artifact"]["id"])
    require(
        record["observation"]["namespace_removed"] is True
        and record["observation"]["source_tree_matched"] is True,
        "witness_schema_invalid",
    )
    runner = record["runner"]
    require(
        set(runner) == {"os", "arch", "environment", "ImageOS", "ImageVersion"}
        and runner["os"] == "Linux"
        and runner["arch"] == "X64"
        and runner["environment"] == "github-hosted",
        "witness_schema_invalid",
    )
    require(
        all(
            runner[key] is None
            or (type(runner[key]) is str and re.fullmatch("[A-Za-z0-9_.-]{1,64}", runner[key]))
            for key in ("ImageOS", "ImageVersion")
        ),
        "witness_schema_invalid",
    )
    setup = decode(files["application-evidence/setup.json"], 256 * 1024)
    require(
        setup["archives"]["source"] == runtime_archive(source_files), "original_source_mismatch"
    )
    for name in FILES:
        if name.endswith(".json"):
            decode(files[name], 256 * 1024)
    return {
        "source_tree": source_tree,
        "original_base": base,
        "original_merge": merge,
        "original_merge_tree": merge_tree,
        "definition_profile": profile,
        "file_count": 25,
        "step_count": 9,
        "product_receipt_files_present": 18,
        "runtime_archive_binding_verified": True,
    }


def verify(request, api, git):
    """Testable internal engine; only the isolated CLI supplies live trusted adapters."""
    try:
        request.validate()
        before = metadata(api, request)
        archives = []
        for item in (before["receipt"], before["witness"]):
            raw = api.get(f"{PREFIX}/actions/artifacts/{item['id']}/zip", binary=True)
            require(
                len(raw) == item["size_in_bytes"] and digest(raw) == item["digest"],
                "artifact_integrity_mismatch",
            )
            archives.append(raw)
        files = archive(archives[0], FILES)
        witness = archive(archives[1], ("execution-witness.json",), witness=True)[
            "execution-witness.json"
        ]
        bound = bind_record(witness, files, request, before, git, api)
        comparison = None
        if request.comparison_source is not None:
            comparison = {
                "source": request.comparison_source,
                "tree_equal": git.tree(request.comparison_source) == bound["source_tree"],
                "execution_claim": False,
            }
        require(metadata(api, request) == before, "context_changed")
        result = {
            "status": "metadata_artifact_binding_verified",
            "metadata_binding_verified": True,
            **BOUNDARIES,
            **bound,
            "repository_id": REPOSITORY_ID,
            "pr": request.pr,
            "original_source": request.source,
            "api_pull_request_association": before["run"]["association"],
            "run": request.run,
            "attempt": 1,
            "receipt_artifact_id": request.receipt_artifact,
            "witness_artifact_id": request.witness_artifact,
            "current_pr_head": before["pr"]["head"],
            "current_pr_base": before["pr"]["base"],
            "comparison": comparison,
            "history_scope": "At most32runs each, original source and current PR head only; no cross-head equivalent-tree veto",
            "original_workload_completed_at": before["job"]["steps"][STEP_NAMES["native"]][
                "completed_at"
            ],
            "verified_at": datetime.now(UTC).isoformat(),
            "failure_codes": [],
        }
        require(
            len(canonical(result)) <= MAX_OUTPUT, "verification_budget_exceeded", unavailable=True
        )
        return result
    except BindingError as error:
        return {
            "status": "unavailable" if error.unavailable else "rejected",
            "metadata_binding_verified": False,
            **BOUNDARIES,
            "failure_codes": [error.code],
        }
    except Exception:
        return {
            "status": "rejected",
            "metadata_binding_verified": False,
            **BOUNDARIES,
            "failure_codes": ["unexpected_input"],
        }


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BindingError("request_invalid")


def exit_status(result):
    # There is deliberately no product/qualification-success exit code.
    return (
        3 if result["metadata_binding_verified"] else 2 if result["status"] == "unavailable" else 1
    )


def failure(code, *, unavailable=False):
    return {
        "status": "unavailable" if unavailable else "rejected",
        "metadata_binding_verified": False,
        **BOUNDARIES,
        "failure_codes": [code],
    }


def main(argv=None):
    parser = Parser(
        description="Metadata/artifact binding ONLY. Exit3 means binding verified; semantic validation is NOT performed and qualification/reuse/authorization remain false. Exit1 rejected; exit2 unavailable."
    )
    for name in ("repository-id", "pr", "run", "attempt", "receipt-artifact", "witness-artifact"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--comparison-source")
    parser.add_argument("--git-repository", required=True)
    parser.add_argument("--output", required=True)
    git, output, budget = None, None, None
    try:
        require(sys.flags.isolated, "trusted_isolation_required", unavailable=True)
        args = parser.parse_args(argv)
        numbers = []
        for name in (
            "repository_id",
            "pr",
            "run",
            "attempt",
            "receipt_artifact",
            "witness_artifact",
        ):
            value = getattr(args, name)
            require(re.fullmatch("[1-9][0-9]{0,18}", value), "request_invalid")
            numbers.append(identity(int(value)))
        request = Request(numbers[0], numbers[1], args.source, *numbers[2:], args.comparison_source)
        request.validate()
        budget = Budget()
        started = budget.clock()
        git = Git(args.git_repository, budget)
        target = Path(args.output).absolute()
        require(
            target.parent.is_dir()
            and not target.exists()
            and not any(
                path.is_symlink() or path.is_junction() for path in (target, *target.parents)
            ),
            "output_invalid",
        )
        require(
            not target.resolve().is_relative_to(git.repository)
            and not target.resolve().is_relative_to(Path(__file__).resolve().parents[2])
            and not any(
                (parent / ".git").exists()
                or ((parent / "HEAD").is_file() and (parent / "objects").is_dir())
                for parent in target.parents
            ),
            "output_invalid",
        )
        output = target
        environment = {**git.environment, "GH_DEBUG": "", "GH_PROMPT_DISABLED": "1"}
        token = (
            process(
                [host_program("gh", git.repository), "auth", "token", "--hostname", "github.com"],
                4096,
                budget,
                env=environment,
                cwd=git._reader,
            )
            .decode("ascii")
            .strip()
        )
        result = verify(request, Github(budget, token), git)
        result["observed_transport"] = {
            "get_attempts": budget.calls,
            "body_bytes": budget.bytes,
            "elapsed_seconds": round(budget.clock() - started, 3),
        }
        budget.remaining()
    except BindingError as error:
        result = failure(error.code, unavailable=error.unavailable)
    except Exception:
        result = failure("unexpected_input", unavailable=True)
    finally:
        if git is not None:
            try:
                git.close()
            except OSError:
                result = failure("local_git_unavailable", unavailable=True)
    if output is not None:
        try:
            raw = canonical(result)
            require(len(raw) <= MAX_OUTPUT, "output_invalid")
            with output.open("xb") as target:
                target.write(raw)
        except (OSError, BindingError):
            result = failure("output_invalid", unavailable=True)
    print(canonical(result).decode(), end="")
    return exit_status(result)
