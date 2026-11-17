"""Public runner receipt and owned-cleanup contracts without external processes."""

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from qualification.application import run_chainsaw as runner
from qualification.application.lease_policy import FACTS as LEASE_FACTS
from qualification.application.result_presentation import EVIDENCE as RESULT_EVIDENCE
from qualification.application.runtime_env_maintenance import FACTS as MAINTENANCE_FACTS
from qualification.application.runtime_env_refusal import CASES, FACTS


def receipt(layer="application_setup", **changes):
    defaults = {}
    if layer == "application_core":
        defaults["lease_policy"] = dict.fromkeys(LEASE_FACTS, True)
        defaults["runtime_env_refusals"] = {case: dict.fromkeys(FACTS, True) for case in CASES}
        defaults["runtime_env_maintenance"] = {
            case: dict.fromkeys(facts, True) for case, facts in MAINTENANCE_FACTS.items()
        }
        defaults["ordinary_results"] = dict(RESULT_EVIDENCE)
        defaults["protected_runtime"] = {
            "api_clear": True,
            "row_ids": [1, 2, 3],
            "started_at": "2026-10-12T00:00:00Z",
        }
    return json.dumps(
        {
            "schema_version": 1,
            "layer": layer,
            "status": "passed",
            "complete_application_gate": False,
            **defaults,
            **changes,
        }
    ).encode()


@pytest.mark.parametrize("failure", ["missing", "partial", "extra", "non-boolean"])
def test_core_receipt_requires_both_exact_negative_runtime_env_cases(failure):
    value = json.loads(receipt("application_core"))
    if failure == "missing":
        del value["runtime_env_refusals"]
    elif failure == "extra":
        value["runtime_env_refusals"]["extra"] = {}
    else:
        value["runtime_env_refusals"][CASES[1]][FACTS[0]] = 1 if failure == "non-boolean" else False
    with pytest.raises(ValueError):
        runner.parse_receipts(json.dumps(value).encode(), ("after-core",))


@pytest.mark.parametrize("failure", ["missing", "false", "extra", "non-boolean"])
@pytest.mark.parametrize("generation", ["before", "after"])
def test_core_receipt_requires_complete_maintenance_in_both_generations(failure, generation):
    value = json.loads(receipt("application_core"))
    if failure == "missing":
        del value["runtime_env_maintenance"]
    elif failure == "extra":
        value["runtime_env_maintenance"]["untrusted"] = "private-value"
    else:
        value["runtime_env_maintenance"]["plaintext_refusal"]["manager_refused"] = (
            1 if failure == "non-boolean" else False
        )
    with pytest.raises(ValueError, match="maintenance evidence"):
        runner.parse_receipts(json.dumps(value).encode(), (f"{generation}-core",))


@pytest.mark.parametrize("failure", ["missing", "false", "extra", "non-boolean"])
@pytest.mark.parametrize("generation", ["before", "after"])
def test_core_receipt_requires_complete_lease_policy_in_both_generations(failure, generation):
    value = json.loads(receipt("application_core"))
    if failure == "missing":
        del value["lease_policy"]
    elif failure == "extra":
        value["lease_policy"]["untrusted"] = "private-value"
    else:
        value["lease_policy"]["startup_refused"] = 1 if failure == "non-boolean" else False
    with pytest.raises(ValueError, match="Lease policy"):
        runner.parse_receipts(json.dumps(value).encode(), (f"{generation}-core",))


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        {},
        {**RESULT_EVIDENCE, "owned_rows_removed": False},
        {**RESULT_EVIDENCE, "current_api": 1},
    ],
)
def test_core_receipts_require_complete_typed_result_presentation_in_both_generations(evidence):
    for generation in ("before", "after"):
        with pytest.raises(ValueError, match="result evidence"):
            runner.parse_receipts(
                receipt("application_core", ordinary_results=evidence),
                (f"{generation}-core",),
            )


@pytest.mark.parametrize("extra", [0, 1])
def test_workflow_receipt_has_its_own_exact_byte_ceiling(extra):
    raw = receipt("workflow_api_admin", observations="")
    raw = receipt(
        "workflow_api_admin",
        observations="x" * (runner.WORKFLOW_RECEIPT_MAX_BYTES - len(raw) + extra),
    )
    assert len(raw) == runner.WORKFLOW_RECEIPT_MAX_BYTES + extra
    if extra:
        with pytest.raises(ValueError):
            runner.parse_receipts(raw, ("before-workflows",))
    else:
        assert runner.parse_receipts(raw, ("before-workflows",)) == {"before-workflows": raw}


def test_receipts_keep_exact_node_bytes_and_skip_nonreceipt_lines():
    nodes = receipt("generic_ray_nodes")
    core = receipt("application_core")
    assert runner.parse_receipts(
        b"Ray connected\n" + nodes + b"\n" + core, ("before-nodes", "before-core")
    ) == {
        "before-nodes": nodes,
        "before-core": core,
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        receipt() + b"\n" + receipt(),
        b"{",
        b"x" * 65537,
        receipt(status="failed"),
        receipt(schema_version=True),
        receipt(complete_application_gate=True),
        receipt("application_core"),
        receipt(unbounded="x" * 16384),
    ],
    ids=[
        "missing",
        "duplicate",
        "malformed",
        "large-log",
        "failed",
        "bool-schema",
        "full-gate",
        "wrong-layer",
        "large-receipt",
    ],
)
def test_receipts_refuse_partial_duplicate_wrong_or_unbounded_evidence(raw):
    with pytest.raises(ValueError):
        runner.parse_receipts(raw, ("setup",))


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "test",
        "timeout",
        "receipt",
        "receipt-type",
        "receipt-import",
        "cleanup",
        "ownership",
        "source",
        "worker-images",
        "worker-diagnostic",
        "worker-diagnostic-invalid",
        "worker-diagnostic-unavailable",
        "worker-diagnostic-collection",
        "web-runtime",
        "landing-asset",
        "landing-timeout",
        "landing-private",
    ],
)
@pytest.mark.parametrize(
    "profile, intent", [("standard", "acceptance"), ("constrained-ray", "diagnostic")]
)
def test_runner_requires_assertions_source_and_owned_namespace_cleanup(
    tmp_path, monkeypatch, capsys, failure, profile, intent
):
    output = tmp_path / "evidence"
    calls = []
    trees = 0
    diagnostic = {
        "schema_version": 2,
        "phase": "ready_sync",
        "category": "validation",
        "timeout_source": None,
        "cleanup_phase": "cleanup_restore",
        "cleanup_category": "timeout",
        "cleanup_timeout_source": "remaining_budget_limit",
        "temporary_removed": True,
        "rows_removed": True,
        "history_preserved": True,
        "original_restored": False,
    }
    private_error = "PRIVATE_WORKER_ERROR_MUST_NOT_BE_RETAINED"

    def checked(argv, *, data=None, timeout=40):
        nonlocal trees
        calls.append(argv)
        if argv[0] == "git":
            if "rev-parse" in argv:
                trees += 1
                return b"b" if failure == "source" and trees > 1 else b"a"
            return b""
        if argv[0] == "chainsaw":
            return b"Version: 0.2.15\n"
        assert argv[:4] == ["kubectl", "--context", "admitted-test", "--request-timeout=30s"]
        if "create" in argv and "-f" in argv:
            secret = json.loads(data)
            assert set(secret["stringData"]) == set(runner.CREDENTIAL_KEYS)
            assert all(len(value) == 43 for value in secret["stringData"].values())
            assert not any(value in " ".join(argv) for value in secret["stringData"].values())
            return b""
        if "delete" in argv:
            assert "--timeout=180s" in argv
            if failure == "cleanup":
                raise RuntimeError("private API response")
            return b""
        if "--ignore-not-found" in argv:
            return b""
        uid = "other" if failure == "ownership" and "get" in argv else "owned"
        return json.dumps({"metadata": {"uid": uid}}).encode()

    def run(argv, *_args):
        assert argv[:2] == ["chainsaw", "test"]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, runner.APPLICATION_SECONDS)
        if failure == "test":
            raise RuntimeError("Test failed")
        return 0.001  # A prior phase's remaining budget must not constrain the next one.

    def worker_images(**kwargs):
        assert "deadline" not in kwargs
        if failure == "worker-images":
            raise ValueError(private_error)
        if failure and failure.startswith("worker-diagnostic"):
            error = runner.WorkerImageObservationError(diagnostic)
            error.args = (private_error,)
            if failure == "worker-diagnostic-invalid":
                monkeypatch.setattr(
                    error, "diagnostic", lambda: {**diagnostic, "raw": private_error}
                )
            elif failure == "worker-diagnostic-unavailable":

                def unavailable():
                    raise ValueError(private_error)

                monkeypatch.setattr(error, "diagnostic", unavailable)
            raise error
        return {"test_fixture": True}

    def collect(*args, expected_profile):
        assert expected_profile == runner.select_profile(profile, intent)
        if failure in {"receipt", "worker-diagnostic-collection"}:
            raise ValueError("Incomplete receipt")
        if failure == "receipt-type":
            raise TypeError("private malformed response")
        if failure == "receipt-import":
            raise ModuleNotFoundError(private_error, name="django_ray")
        if failure == "web-runtime":
            return None
        return runner.observe_web_runtime(web_pod(), "example/core@sha256:" + "a" * 64)

    def landing_asset(_kubectl, _namespace, web, _image):
        if failure == "landing-asset":
            return {"private": private_error}
        if failure == "landing-timeout":
            raise subprocess.TimeoutExpired(["fixed-landing-observer"], 15)
        if failure == "landing-private":
            raise ValueError(private_error)
        return {
            "schema_version": 1,
            "status": "passed",
            "asset": "testproject/landing.js",
            "path": "/static/testproject/landing.js",
            "bytes": 1,
            "sha256": "a" * 64,
            **{key: web[key] for key in ("pod", "uid", "container")},
        }

    monkeypatch.setattr(runner, "checked", checked)
    monkeypatch.setattr(runner, "run_test", run)
    monkeypatch.setattr(runner, "observe_worker_images", worker_images)
    monkeypatch.setattr(runner, "collect", collect)
    monkeypatch.setattr(runner, "observe_landing_asset", landing_asset)
    monkeypatch.setattr(runner, "diagnose", lambda *_args: None)
    result = runner.main(
        [
            "--context",
            "admitted-test",
            "--image",
            "example/core@sha256:" + "a" * 64,
            "--storage-class",
            "standard",
            "--output",
            str(output),
            "--resource-profile",
            profile,
            "--validation-intent",
            intent,
        ]
    )
    summary = json.loads((output / "summary.json").read_text())
    assert result == (0 if failure is None else 1)
    assert summary["status"] == ("passed" if failure is None else "failed")
    assert summary["complete_application_gate"] is False
    assert summary["namespace_removed"] is (failure not in {"cleanup", "ownership"})
    assert summary["qualification_profile"] == runner.select_profile(profile, intent)
    if summary["web_runtime"] is not None and not (failure and failure.startswith("landing-")):
        runner.landing_asset_contract.validate_bound_record(
            summary["landing_asset"], summary["web_runtime"]
        )
    else:
        assert summary["landing_asset"] is None
    if failure not in {
        "receipt",
        "receipt-type",
        "receipt-import",
        "worker-diagnostic-collection",
        "web-runtime",
    }:
        assert summary["web_runtime"] == runner.observe_web_runtime(
            web_pod(), "example/core@sha256:" + "a" * 64
        )
    else:
        assert summary["web_runtime"] is None
    assert summary["worker_images"] == (
        None
        if failure in {"test", "timeout", "worker-images"}
        or (failure and failure.startswith("worker-diagnostic"))
        else {"test_fixture": True}
    )
    if failure in {"worker-diagnostic", "worker-diagnostic-collection"}:
        assert summary["worker_images_failure"] == diagnostic
    else:
        assert "worker_images_failure" not in summary
    values = json.loads((output / "values.json").read_text())
    assert values["resourceProfile"] == profile
    assert values["validationIntent"] == intent
    assert values["rayHeadLogicalCPU"] == ("0" if profile == "constrained-ray" else "1")
    assert any("delete" in call for call in calls) is (failure != "ownership")
    assert not (output / "secret.json").exists()
    captured = capsys.readouterr()
    assert private_error not in captured.out + captured.err
    assert all(private_error.encode() not in path.read_bytes() for path in output.iterdir())


def test_constrained_acceptance_rejected_before_commands_or_output(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "checked", lambda *_a, **_k: pytest.fail("must reject first"))
    output = tmp_path / "evidence"
    with pytest.raises(SystemExit):
        runner.main(
            [
                "--context",
                "unused",
                "--image",
                "example/core@sha256:" + "a" * 64,
                "--storage-class",
                "standard",
                "--output",
                str(output),
                "--resource-profile",
                "constrained-ray",
            ]
        )
    assert not output.exists()


def test_runner_rejects_mutable_image_before_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "checked", lambda *_args, **_kwargs: pytest.fail("command before image validation")
    )
    with pytest.raises(SystemExit):
        runner.main(
            [
                "--context",
                "unused",
                "--image",
                "example/core:latest",
                "--storage-class",
                "standard",
                "--output",
                str(tmp_path / "evidence"),
            ]
        )


def test_collector_rejects_a_different_running_image_digest(tmp_path, monkeypatch):
    image = "example/core@sha256:" + "a" * 64
    pod = web_pod()
    pod["spec"]["initContainers"] = [{"name": "setup", "image": image}]
    pod["status"]["initContainerStatuses"] = [
        {
            "name": "setup",
            "restartCount": 0,
            "imageID": "example/core@sha256:" + "b" * 64,
            "state": {"terminated": {"exitCode": 0}},
        }
    ]
    monkeypatch.setattr(
        runner,
        "checked",
        lambda argv: receipt() if "logs" in argv else json.dumps({"items": [pod]}).encode(),
    )
    with pytest.raises(ValueError, match="candidate container"):
        runner.collect(["kubectl"], "owned", tmp_path, image)
    assert not (tmp_path / "setup.json").exists()


def web_pod():
    image = "example/core@sha256:" + "a" * 64
    return {
        "metadata": {
            "name": "django-web-fixture",
            "uid": "11111111-1111-4111-8111-111111111111",
            "labels": {"app": "django-web"},
        },
        "spec": {"containers": [{"name": "web", "image": image}]},
        "status": {
            "containerStatuses": [
                {
                    "name": "web",
                    "imageID": "containerd://" + image,
                    "ready": True,
                    "restartCount": 0,
                    "state": {"running": {}},
                }
            ]
        },
    }


@pytest.mark.parametrize("failure", [None, "digest", "oversized", "timeout", "uid", "pod", "image"])
def test_landing_asset_probe_binds_source_and_the_same_named_web_pod(
    tmp_path, monkeypatch, failure
):
    image = "example/core@sha256:" + "a" * 64
    pod = web_pod()
    web = runner.observe_web_runtime(pod, image)
    source = tmp_path / runner.landing_asset_contract.SOURCE_RELATIVE
    source.parent.mkdir(parents=True)
    source.write_bytes(b"const landing = 'source';\n")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    value = runner.landing_asset_contract.source_record(source)
    if failure == "digest":
        value["sha256"] = "b" * 64
    calls = []

    def checked(argv, *, timeout):
        calls.append(argv)
        assert timeout == 15
        if "exec" in argv:
            assert argv == [
                "kubectl",
                "exec",
                web["pod"],
                "-n",
                "owned",
                "-c",
                "web",
                "--",
                "python",
                "-m",
                "qualification.application.landing_asset",
            ]
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 15)
            if failure == "oversized":
                return b"private-canary" + b"x" * 4096
            return json.dumps(value).encode()
        assert argv == ["kubectl", "get", "pod", web["pod"], "-n", "owned", "-o", "json"]
        if failure == "uid":
            pod["metadata"]["uid"] = "22222222-2222-4222-8222-222222222222"
        elif failure == "pod":
            pod["metadata"]["name"] = "django-web-replacement"
        elif failure == "image":
            pod["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:" + "b" * 64
        return json.dumps(pod).encode()

    monkeypatch.setattr(runner, "checked", checked)
    if failure is None:
        result = runner.observe_landing_asset(["kubectl"], "owned", web, image)
        assert result == {**value, **{key: web[key] for key in ("pod", "uid", "container")}}
        assert len(calls) == 2
    else:
        with pytest.raises((ValueError, subprocess.TimeoutExpired)) as raised:
            runner.observe_landing_asset(["kubectl"], "owned", web, image)
        assert "private-canary" not in str(raised.value)
        assert len(calls) == (1 if failure in {"digest", "oversized", "timeout"} else 2)


@pytest.mark.parametrize("field,value", [("ready", False), ("container", "setup"), ("uid", "bad")])
def test_landing_asset_refuses_an_unvalidated_web_snapshot_before_exec(monkeypatch, field, value):
    image = "example/core@sha256:" + "a" * 64
    web = runner.observe_web_runtime(web_pod(), image)
    web[field] = value
    monkeypatch.setattr(runner, "checked", lambda *_a, **_k: pytest.fail("exec before identity"))
    with pytest.raises(ValueError):
        runner.observe_landing_asset(["kubectl"], "owned", web, image)


@pytest.mark.parametrize(
    "field,value",
    [
        ("imageID", ""),
        ("imageID", "example/core@sha256:" + "b" * 64),
        ("ready", False),
        ("ready", 1),
        ("restartCount", False),
        ("restartCount", 1),
        ("state", {"waiting": {}}),
        ("state", {"running": {}, "terminated": {}}),
        ("state", {"running": None}),
        ("state", {"running": False}),
        ("state", ["running"]),
    ],
)
def test_regular_web_snapshot_refuses_incomplete_or_wrong_runtime(field, value):
    pod = web_pod()
    pod["status"]["containerStatuses"][0][field] = value
    with pytest.raises(ValueError, match="Regular web"):
        runner.observe_web_runtime(pod, "example/core@sha256:" + "a" * 64)


@pytest.mark.parametrize("prefix", ["", "containerd://", "docker-pullable://"])
@pytest.mark.parametrize("repository", ["", "example/core@"])
def test_web_runtime_accepts_only_anchored_documented_digest_forms(prefix, repository):
    pod = web_pod()
    pod["status"]["containerStatuses"][0]["imageID"] = prefix + repository + "sha256:" + "a" * 64
    assert runner.observe_web_runtime(pod, "example/core@sha256:" + "a" * 64)["ready"] is True


@pytest.mark.parametrize(
    "image_id",
    [
        "unsupported://example/core@sha256:" + "a" * 64,
        "garbage-sha256:" + "a" * 64,
        "example/core@@sha256:" + "a" * 64,
        "sha256:" + "a" * 64 + "suffix",
    ],
)
def test_web_runtime_does_not_accept_arbitrary_digest_suffixes(image_id):
    pod = web_pod()
    pod["status"]["containerStatuses"][0]["imageID"] = image_id
    with pytest.raises(ValueError, match="Regular web"):
        runner.observe_web_runtime(pod, "example/core@sha256:" + "a" * 64)


@pytest.mark.parametrize(
    "kind",
    [
        "missing-spec",
        "duplicate-spec",
        "missing-status",
        "duplicate-status",
        "deleting",
        "wrong-spec-image",
    ],
)
def test_regular_web_is_selected_by_its_unique_name(kind):
    pod = web_pod()
    if kind == "deleting":
        pod["metadata"]["deletionTimestamp"] = "present"
    elif kind == "wrong-spec-image":
        pod["spec"]["containers"][0]["image"] = "another/image:latest"
    else:
        entries = (
            pod["spec"]["containers"]
            if kind.endswith("spec")
            else pod["status"]["containerStatuses"]
        )
        if kind.startswith("missing"):
            entries.clear()
        else:
            entries.append(deepcopy(entries[0]))
    with pytest.raises(ValueError):
        runner.observe_web_runtime(pod, "example/core@sha256:" + "a" * 64)


def test_collector_keeps_four_producers_and_returns_separate_regular_web_snapshot(
    tmp_path, monkeypatch
):
    image = "example/core@sha256:" + "a" * 64
    pods = []
    for number, (app, (container, _names)) in enumerate(runner.RECEIPTS.items(), 1):
        pod = (
            web_pod()
            if app == "django-web"
            else {
                "metadata": {"name": app + "-fixture", "uid": str(number), "labels": {"app": app}},
                "spec": {},
                "status": {},
            }
        )
        spec_key = "initContainers" if app == "django-web" else "containers"
        status_key = "initContainerStatuses" if app == "django-web" else "containerStatuses"
        pod["spec"][spec_key] = [{"name": container, "image": image}]
        pod["status"][status_key] = [
            {
                "name": container,
                "imageID": "containerd://" + image,
                "restartCount": 0,
                "state": {"terminated": {"exitCode": 0}},
            }
        ]
        pods.append(pod)
    calls = []

    def checked(argv):
        calls.append(argv)
        return b"{}" if "logs" in argv else json.dumps({"items": pods}).encode()

    before = b"before nodes"
    after = json.dumps(
        {"cold_replacement": True, "previous_receipt_sha256": hashlib.sha256(before).hexdigest()}
    ).encode()
    monkeypatch.setattr(runner, "checked", checked)
    live_receipts = current_asset_receipts()
    monkeypatch.setattr(
        runner,
        "parse_receipts",
        lambda _raw, names, **_kw: {
            name: before
            if name == "before-nodes"
            else after
            if name == "after-nodes"
            else live_receipts.get(name, b"{}")
            for name in names
        },
    )
    from qualification.application import protected_logs

    monkeypatch.setattr(protected_logs, "validate_receipt", lambda _raw, _generation: None)
    for generation in ("before", "after"):
        (tmp_path / f"{generation}-protected-logs.json").write_bytes(b"{}")
    result = runner.collect(["kubectl"], "owned", tmp_path, image)
    producers = json.loads((tmp_path / "producers.json").read_bytes())
    assert len(producers) == 4
    assert [p["container"] for p in producers] == [
        "setup",
        "assertions",
        "assertions",
        "assertions",
    ]
    assert result["container"] == "web"
    assert result["uid"] == producers[0]["uid"]
    assert result["image_id"] == "containerd://" + image
    assert sum("get" in call for call in calls) == 1
    assert sum("logs" in call for call in calls) == 4


def current_asset_receipts():
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures/test_planning_receipts/files.json"
        ).read_bytes()
    )
    assets = {
        "task_live.js": {"bytes": 5725, "sha256": "a" * 64},
        "workflow_diagnostics.js": {"bytes": 56730, "sha256": "b" * 64},
    }

    def augment(value):
        if isinstance(value, dict):
            if "browser_contract" in value:
                value["browser_contract"]["admin_assets"] = deepcopy(assets)
            for child in value.values():
                augment(child)
        elif isinstance(value, list):
            for child in value:
                augment(child)

    result = {}
    for path, raw in fixture.items():
        name = path.rsplit("/", 1)[-1].removesuffix(".json")
        if name in {
            f"{generation}-{layer}"
            for generation in ("before", "after")
            for layer in ("first", "workflows", "showcase")
        }:
            value = json.loads(raw)
            augment(value)
            result[name] = json.dumps(value).encode()
    return result


def test_live_asset_collection_needs_no_application_dependencies(tmp_path):
    receipts = tmp_path / "receipts.json"
    receipts.write_text(
        json.dumps({name: raw.decode() for name, raw in current_asset_receipts().items()}),
        encoding="utf-8",
    )
    script = """
import importlib.abc
import json
import sys
from pathlib import Path

blocked = {"django_ray", "django", "ray"}
assert not any(name.split(".")[0] in blocked for name in sys.modules)
class DenyApplicationImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise ModuleNotFoundError("Application dependency denied", name=fullname)
sys.meta_path.insert(0, DenyApplicationImports())
sys.path.insert(0, sys.argv[1])
from qualification.application.run_chainsaw import validate_live_admin_assets
receipts = {
    name: raw.encode() for name, raw in json.loads(Path(sys.argv[2]).read_bytes()).items()
}
assert len(receipts) == 6
validate_live_admin_assets(receipts)
from qualification.application.workflow_fixtures import workflow_cases
assert len(workflow_cases()) == 12
assert sum(len(case.states) for case in workflow_cases()) == 16
assert not any(name.split(".")[0] in blocked for name in sys.modules)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(Path(__file__).resolve().parents[2]),
            str(receipts),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_workflow_driver_reexports_the_canonical_fixture_types_and_cases():
    from qualification.application import run_workflows, workflow_fixtures

    assert run_workflows.WorkflowCase is workflow_fixtures.WorkflowCase
    assert run_workflows.workflow_cases is workflow_fixtures.workflow_cases
    assert all(
        type(case) is workflow_fixtures.WorkflowCase
        for case in (
            *run_workflows.workflow_cases(),
            *run_workflows.first_workflow_cases(),
            run_workflows.showcase_failure_case(),
        )
    )


@pytest.mark.parametrize("generation", ["before", "after"])
@pytest.mark.parametrize("layer", ["first", "workflows", "showcase"])
@pytest.mark.parametrize("failure", ["missing", "length", "digest", "extra", "case"])
def test_live_collection_requires_each_case_identity_in_both_generations(
    generation, layer, failure
):
    receipts = current_asset_receipts()
    runner.validate_live_admin_assets(receipts)
    name = f"{generation}-{layer}"
    value = json.loads(receipts[name])
    if layer == "first":
        target = value["trials"][-1]["observations"][-1]
    elif layer == "workflows":
        target = value["observations"]["recovery"][-1]
    else:
        target = value["observation"]
    if failure == "case":
        if layer == "first":
            value["trials"].pop()
        elif layer == "workflows":
            del value["observations"]["recovery"]
        else:
            del value["observation"]
    elif failure == "missing":
        del target["browser_contract"]["admin_assets"]
    elif failure == "extra":
        target["browser_contract"]["admin_assets"]["other.js"] = {"bytes": 1, "sha256": "c" * 64}
    else:
        key, replacement = ("bytes", 1) if failure == "length" else ("sha256", "c" * 64)
        target["browser_contract"]["admin_assets"]["task_live.js"][key] = replacement
    receipts[name] = json.dumps(value).encode()
    with pytest.raises(ValueError, match="Admin asset"):
        runner.validate_live_admin_assets(receipts)


def test_live_collection_refuses_legacy_assets_and_assets_on_the_wrong_attempt():
    receipts = current_asset_receipts()
    del receipts["after-workflows"]
    with pytest.raises(ValueError, match="Admin asset"):
        runner.validate_live_admin_assets(receipts)
    receipts = current_asset_receipts()
    value = json.loads(receipts["after-workflows"])
    attempts = value["observations"]["recovery"]
    attempts[0]["browser_contract"] = attempts[-1].pop("browser_contract")
    receipts["after-workflows"] = json.dumps(value).encode()
    with pytest.raises(ValueError, match="Admin asset"):
        runner.validate_live_admin_assets(receipts)


def test_failure_diagnostics_exclude_resource_specs_and_bound_records(tmp_path, monkeypatch):
    item = {
        "metadata": {"name": "ray", "annotations": {"private": "excluded"}},
        "spec": {"private": "excluded"},
        "status": {"state": "pending"},
        "reason": "FailedCreate",
        "message": "Pod validation failed",
    }
    monkeypatch.setattr(
        runner, "checked", lambda _argv: json.dumps({"items": [item] * 101}).encode()
    )
    runner.diagnose(["kubectl"], "owned", tmp_path)
    for path in tmp_path.iterdir():
        assert len(json.loads(path.read_bytes())) == 100
        assert b"excluded" not in path.read_bytes()
    events = json.loads((tmp_path / "diagnostic-events.json").read_bytes())
    assert events[0]["reason"] == "FailedCreate"


def test_failure_diagnostics_never_collect_raw_application_logs(tmp_path, monkeypatch):
    names = [
        "assert-before-one",
        "assert-after-one",
        "django-web-one",
        "django-manager-one",
        *(f"ray-{i}" for i in range(5)),
    ]
    pods = [
        {
            "metadata": {"name": name},
            "status": {"containerStatuses": [{"name": "main", "restartCount": 1}]},
        }
        for name in names
    ]

    def checked(argv, **kwargs):
        if "logs" in argv:
            return b"x" * 40000
        return json.dumps({"items": pods if "pods" in argv else []}).encode()

    monkeypatch.setattr(runner, "checked", checked)
    runner.diagnose(["kubectl"], "owned", tmp_path)
    assert not list(tmp_path.glob("*.log"))
    assert (tmp_path / "diagnostic-pods.json").exists()


@pytest.mark.parametrize("failure", [None, "timeout", "oversized", "invalid"])
def test_actor_diagnostics_are_bounded_and_cannot_prevent_owned_cleanup(
    tmp_path, monkeypatch, failure
):
    pods = [{"metadata": {"name": "ray-owned-head", "labels": {"app": "ray-head"}}}]
    calls = []

    def checked(argv, *, timeout):
        calls.append(argv)
        assert timeout == 15
        assert argv[:3] == ["kubectl", "exec", "--request-timeout=5s"]
        assert argv[argv.index("--") + 1 : -1] == [
            "timeout",
            "--signal=TERM",
            "--kill-after=1s",
            "12s",
            "python",
            "-c",
        ]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if failure == "invalid":
            return b"[]"
        return (
            b"x" * 16385
            if failure == "oversized"
            else json.dumps(
                {
                    "diagnostic_only": True,
                    "status": "unavailable",
                    "resources": {
                        kind: {"status": "unavailable", "records": []}
                        for kind in ("actors", "tasks", "nodes")
                    },
                }
            ).encode()
        )

    monkeypatch.setattr(runner, "checked", checked)
    runner.diagnose_progress_actors(["kubectl"], "owned", tmp_path, pods)
    assert len(calls) == 1
    assert (tmp_path / "diagnostic-progress-actors.json").exists() is (failure is None)
    runner.diagnose_progress_actors(["kubectl"], "owned", tmp_path, pods * 2)
    assert len(calls) == 1


def test_failure_diagnostics_do_not_prevent_cleanup_on_api_failure(tmp_path, monkeypatch):
    def failed(_argv):
        raise RuntimeError("private API response")

    monkeypatch.setattr(runner, "checked", failed)
    runner.diagnose(["kubectl"], "owned", tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("completion_seconds", "ignore_terminate"),
    [(2161, False), (2819.5, False), (2821, False), (2821, True)],
)
def test_accounted_outer_budget_completes_or_stops_at_its_bound(
    monkeypatch, completion_seconds, ignore_terminate
):
    clock = [100.0]
    started = clock[0]
    monitored_waits = []
    cleanup_waits = []
    health_checks = []

    class Process:
        stopped = False
        terminated_at = None
        killed = False

        def wait(self, timeout):
            if self.terminated_at is not None:
                cleanup_waits.append(timeout)
                if ignore_terminate and not self.killed:
                    clock[0] += timeout
                    raise subprocess.TimeoutExpired("chainsaw", timeout)
                self.stopped = True
                return 0
            assert 0 < timeout <= 15
            monitored_waits.append(timeout)
            remaining = started + completion_seconds - clock[0]
            if remaining <= timeout:
                clock[0] += remaining
                self.stopped = True
                return 0
            clock[0] += timeout
            raise subprocess.TimeoutExpired("chainsaw", timeout)

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.terminated_at = clock[0] - started

        def kill(self):
            self.killed = True

    process = Process()

    def checked(argv):
        assert argv[:2] == ["kubectl", "get"]
        assert argv[2] in {"pods", "rayclusters"}
        assert argv[3:] == ["-n", "owned", "-o", "json"]
        health_checks.append(argv[2])
        return b'{"items": []}'

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(runner, "checked", checked)
    if completion_seconds < 2820:
        runner.run_test(["chainsaw", "test"], ["kubectl"], "owned")
        assert clock[0] - started == completion_seconds > 2160
        assert process.terminated_at is None
        assert cleanup_waits == []
    else:
        with pytest.raises(TimeoutError, match="accounted wall-clock"):
            runner.run_test(["chainsaw", "test"], ["kubectl"], "owned")
        assert sum(monitored_waits) == 2820
        assert process.terminated_at == 2820
        assert cleanup_waits == ([10, 10] if ignore_terminate else [10])
    assert health_checks
    assert process.stopped
    assert process.killed is ignore_terminate


@pytest.mark.parametrize("deleting", [None, "pod", "owner", "owner-absent"])
@pytest.mark.parametrize(
    "exit_code, expected", [(137, 137), ("private-canary", None), (True, None)]
)
def test_container_failure_stops_chainsaw_but_allows_planned_deletion(
    monkeypatch, capsys, deleting, exit_code, expected
):
    class Process:
        stopped = False
        waits = 0

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                assert timeout == 15
                raise subprocess.TimeoutExpired("chainsaw", timeout)
            self.stopped = True
            return 0

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

    process = Process()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)
    pod = {
        "metadata": {
            "name": "private-canary-pod",
            "ownerReferences": [{"kind": "RayCluster", "uid": "generation"}],
            **({"deletionTimestamp": "now"} if deleting == "pod" else {}),
        },
        "status": {
            "containerStatuses": [
                {
                    "name": "private-canary-container",
                    "lastState": {
                        "terminated": {"exitCode": exit_code, "reason": "private-canary-reason"}
                    },
                }
            ]
        },
    }
    cluster = {
        "metadata": {
            "uid": "generation",
            **({"deletionTimestamp": "now"} if deleting == "owner" else {}),
        }
    }

    def checked(argv):
        items = (
            ([cluster] if deleting != "owner-absent" else []) if "rayclusters" in argv else [pod]
        )
        return json.dumps({"items": items}).encode()

    monkeypatch.setattr(runner, "checked", checked)
    if deleting:
        runner.run_test(["chainsaw", "test"], ["kubectl"], "owned")
    else:
        with pytest.raises(RuntimeError, match="container failed"):
            runner.run_test(["chainsaw", "test"], ["kubectl"], "owned")
    assert process.stopped
    output = capsys.readouterr().out
    assert "private-canary" not in output
    if deleting:
        assert not output
    else:
        assert json.loads(output) == {"application_container_failed": True, "exit_code": expected}
