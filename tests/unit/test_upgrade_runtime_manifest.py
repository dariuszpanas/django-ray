"""Finite host rendering, without a build, Kubernetes client, database or Ray."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from qualification.upgrade import runtime_manifest as renderer


def config():
    return renderer.RuntimeManifestConfig(
        namespace="upgrade-owned",
        admitted_node="admitted-node.example",
        storage_class="standard",
        postgres_image="postgres@sha256:" + "a" * 64,
        baseline_image="localhost:5001/upgrade/old@sha256:" + "b" * 64,
        candidate_image="localhost:5001/upgrade/current@sha256:" + "c" * 64,
        baseline_python_version="3.12.14",
        candidate_python_version="3.12.14",
        postgres_user="upgrade",
        primary_database="upgrade_primary",
        scratch_database="upgrade_scratch",
    )


def render(template="manager", **kwargs):
    options = {"build": "candidate", "manager_mode": "core", "job_name": "upgrade-manager"}
    if template != "manager":
        options = {}
    options.update(kwargs)
    return renderer.render_runtime_manifest(template, config(), **options)


@pytest.mark.parametrize("build", ["baseline", "candidate"])
@pytest.mark.parametrize("mode", ["core", "jobs"])
def test_exact_manager_modes_and_images(build, mode):
    value = render(build=build, manager_mode=mode)
    (container,) = value["spec"]["template"]["spec"]["containers"]
    expected = ["--queue", "upgrade-" + mode, "--concurrency", "1"]
    if mode == "core":
        expected.append("--cluster=ray-head:6379")
    assert container["command"] == ["python", "-m", "django", "django_ray_worker"]
    assert container["args"] == expected
    assert container["image"] == getattr(config(), build + "_image")
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["DJANGO_RAY_UPGRADE_BUILD"] == build
    assert env["DJANGO_RAY_UPGRADE_DATABASE"] == "primary"
    assert env["DJANGO_SETTINGS_MODULE"] == "qualification.upgrade.runtime_settings"
    assert env["DJANGO_RAY_UPGRADE_CORE_ADDRESS"] == "ray-head:6379"
    assert env["DJANGO_RAY_UPGRADE_JOBS_ADDRESS"] == "http://ray-head:8265"
    assert "RAY_ADDRESS" not in env
    assert not any("${" in str(item) for item in container.values())
    assert json.loads(json.dumps(value)) == value


@pytest.mark.parametrize(
    "template", ["postgres_pvc", "artifact_pvc", "postgres_service", "postgres", "ray_service"]
)
def test_infrastructure_templates_only_return_owned_plain_objects(template, monkeypatch):
    import subprocess

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("renderer launched a process")
    )
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("renderer launched a process")
    )
    value = render(template)
    assert value["metadata"]["namespace"] == "upgrade-owned"
    assert "${" not in json.dumps(value)
    assert yaml.safe_load(yaml.safe_dump(value)) == value


@pytest.mark.parametrize("build,ray_version", [("baseline", "2.56.0"), ("candidate", "2.58.0")])
def test_every_ray_node_uses_exact_same_epoch_and_primary_database(build, ray_version):
    spec = render("ray", build=build)["spec"]
    assert spec["rayVersion"] == ray_version
    groups = [spec["headGroupSpec"], *spec["workerGroupSpecs"]]
    assert len(groups) == 2
    for group in groups:
        pod = group["template"]["spec"]
        assert pod["nodeSelector"] == {"kubernetes.io/hostname": config().admitted_node}
        (container,) = pod["containers"]
        assert container["image"] == getattr(config(), build + "_image")
        env = {item["name"]: item["value"] for item in container["env"]}
        assert env["DJANGO_RAY_UPGRADE_DATABASE"] == "primary"
        assert env["DJANGO_SETTINGS_MODULE"] == "qualification.upgrade.runtime_settings"


@pytest.mark.parametrize(
    "action,case,build,database,restore_point",
    [
        ("prepare", None, "baseline", "primary", None),
        ("enqueue", "old-success", "baseline", "primary", None),
        ("enqueue", "current-core", "candidate", "primary", None),
        ("inspect", "old-gated", "candidate", "scratch", "blocked"),
        ("cancel", "old-cancel", "baseline", "primary", None),
        ("release", "old-gated", "baseline", "primary", None),
        ("release", "current-jobs", "candidate", "primary", None),
        ("history", None, "baseline", "primary", None),
        ("compare-history", None, "candidate", "scratch", "final"),
        ("migrate", None, "baseline", "primary", None),
        ("migrate", None, "candidate", "primary", None),
        ("blocked-history", None, "baseline", "scratch", "blocked"),
        ("blocked-migrate", None, "candidate", "scratch", "blocked"),
    ],
)
def test_observer_argv_and_scratch_are_finite(action, case, build, database, restore_point):
    value = render(
        "observer",
        action=action,
        case=case,
        build=build,
        database=database,
        job_name="upgrade-observer",
        restore_point=restore_point,
    )
    (container,) = value["spec"]["template"]["spec"]["containers"]
    assert container["command"] == ["python", "-m", "qualification.upgrade.runtime_steps"]
    assert container["args"] == ([action] if case is None else [action, "--case", case])
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["DJANGO_RAY_UPGRADE_DATABASE"] == database
    assert env["DJANGO_SETTINGS_MODULE"] == "qualification.upgrade.runtime_settings"
    assert env["DJANGO_RAY_UPGRADE_ARTIFACT_ROOT"] == "/artifacts"
    expected_mount = {"name": "artifacts", "mountPath": "/artifacts"}
    if restore_point is not None:
        expected_mount["subPath"] = ".upgrade-restores/" + restore_point
    assert [mount for mount in container["volumeMounts"] if mount["name"] == "artifacts"] == [
        expected_mount
    ]
    assert {
        key for key in env if "PASSWORD" in key or "SECRET_KEY" in key or "ENCRYPTION_KEY" in key
    } == {
        "DJANGO_RAY_UPGRADE_POSTGRES_PASSWORD_FILE",
        "DJANGO_RAY_UPGRADE_ENCRYPTION_KEY_FILE",
        "DJANGO_SECRET_KEY_FILE",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace", "another/namespace"),
        ("namespace", "MixedCase"),
        ("namespace", "x" * 64),
        ("admitted_node", "*.example"),
        ("admitted_node", "node\x00other"),
        ("storage_class", "../standard"),
        ("postgres_image", "postgres:17"),
        ("baseline_image", "registry/a:tag@sha256:" + "a" * 64),
        ("baseline_image", "https://registry/a@sha256:" + "a" * 64),
        ("baseline_image", "registry:65536/a@sha256:" + "a" * 64),
        ("candidate_image", "registry/a@sha256:" + "B" * 64),
        ("candidate_image", "other/alias@sha256:" + "b" * 64),
        ("baseline_python_version", "3.12"),
        ("candidate_python_version", "3.15.1"),
        ("postgres_user", "upgrade;drop database other"),
        ("primary_database", "postgres db"),
        ("scratch_database", "upgrade_primary"),
        ("namespace", None),
    ],
)
def test_malformed_or_crossed_configuration_is_refused(field, value):
    selected = replace(config(), **{field: value})
    with pytest.raises(renderer.RuntimeManifestError, match="invalid-native-upgrade-manifest"):
        renderer.render_runtime_manifest("postgres_pvc", selected)


@pytest.mark.parametrize(
    "template,options",
    [
        ("unknown", {}),
        ("postgres", {"build": "baseline"}),
        ("postgres_pvc", {"job_name": "unexpected"}),
        ("ray", {"build": "candidate", "database": "scratch"}),
        ("manager", {"build": "candidate", "manager_mode": "shell", "job_name": "manager"}),
        ("manager", {"build": "candidate", "manager_mode": "jobs", "job_name": "../manager"}),
        (
            "manager",
            {
                "build": "candidate",
                "manager_mode": "jobs",
                "job_name": "manager",
                "case": "old-success",
            },
        ),
        (
            "observer",
            {
                "build": "candidate",
                "database": "primary",
                "job_name": "observer",
                "action": "migrate-0034",
            },
        ),
        (
            "observer",
            {
                "build": "candidate",
                "database": "primary",
                "job_name": "observer",
                "action": "inspect",
            },
        ),
        (
            "observer",
            {
                "build": "candidate",
                "database": "primary",
                "job_name": "observer",
                "action": "enqueue",
                "case": "old-success",
            },
        ),
        (
            "observer",
            {
                "build": "baseline",
                "database": "scratch",
                "job_name": "observer",
                "action": "enqueue",
                "case": "old-success",
            },
        ),
        (
            "observer",
            {
                "build": "candidate",
                "database": "primary",
                "job_name": "observer",
                "action": "prepare",
                "case": "current-core",
            },
        ),
        (
            "observer",
            {
                "build": "candidate",
                "database": "primary",
                "job_name": "observer",
                "action": "cancel",
                "case": "current-jobs",
            },
        ),
        (
            "observer",
            {
                "build": "baseline",
                "database": "primary",
                "job_name": "observer",
                "action": "release",
                "case": "old-cancel",
            },
        ),
    ],
)
def test_extra_options_and_unsupported_actions_are_refused(template, options):
    with pytest.raises(renderer.RuntimeManifestError):
        renderer.render_runtime_manifest(template, config(), **options)


@pytest.mark.parametrize(
    "fault",
    [
        "embedded",
        "unknown",
        "extra-env",
        "missing-env",
        "duplicate-env",
        "secret-env",
        "image",
        "env-from",
        "settings-module",
    ],
)
def test_modified_template_cannot_smuggle_environment_or_substitution(tmp_path, monkeypatch, fault):
    data = yaml.safe_load(renderer._PROFILE.read_text())
    manager = data["templates"]["manager"]["spec"]["template"]["spec"]["containers"][0]
    if fault == "embedded":
        manager["args"] = "prefix-${managerArgs}"
    elif fault == "unknown":
        manager["args"] = "${arbitraryArgv}"
    elif fault == "extra-env":
        manager["env"].append({"name": "RAY_ADDRESS", "value": "another:6379"})
    elif fault == "missing-env":
        del manager["env"]
    elif fault == "duplicate-env":
        manager["env"].append(manager["env"][0])
    elif fault == "secret-env":
        row = next(row for row in manager["env"] if row["name"] == "DJANGO_SECRET_KEY_FILE")
        row["name"] = "DJANGO_SECRET_KEY"
    elif fault == "image":
        manager["image"] = config().baseline_image
    elif fault == "settings-module":
        row = next(row for row in manager["env"] if row["name"] == "DJANGO_SETTINGS_MODULE")
        row["value"] = "qualification.upgrade.runtime_history_settings"
    else:
        manager["envFrom"] = [{"secretRef": {"name": "another-secret"}}]
    profile = tmp_path / "runtime.yaml"
    profile.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(renderer, "_PROFILE", profile)
    with pytest.raises(renderer.RuntimeManifestError):
        render()


def test_duplicate_yaml_keys_and_oversized_source_are_refused(tmp_path, monkeypatch):
    original = renderer._PROFILE.read_bytes()
    profile = tmp_path / "runtime.yaml"
    monkeypatch.setattr(renderer, "_PROFILE", profile)
    for raw in (original + b"\nschema: 1\n", b"#" * (64 * 1024 + 1)):
        profile.write_bytes(raw)
        with pytest.raises(renderer.RuntimeManifestError):
            render()


def test_missing_or_extra_replacements_are_refused():
    for replacements in (
        {},
        {"namespace": "owned", "extra": "forbidden"},
        {"namespace": "${injected}"},
    ):
        with pytest.raises(renderer.RuntimeManifestError):
            renderer._substitute({"name": "${namespace}"}, replacements)


def test_rendered_objects_are_independent():
    first = render("ray", build="candidate")
    first["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["env"].clear()
    second = render("ray", build="candidate")
    assert second["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["env"]
    assert first["spec"]["workerGroupSpecs"][0]["template"]["spec"]["containers"][0]["env"]


def test_renderer_has_no_cluster_or_runtime_imports():
    source = Path(renderer.__file__).read_text()
    assert all(
        name not in source
        for name in ("import subprocess", "import django", "import ray", "import kubernetes")
    )
    untyped_render: Any = renderer.render_runtime_manifest
    with pytest.raises(TypeError):
        untyped_render("manager", config(), argv=["sh", "-c", "anything"])


@pytest.mark.parametrize(
    "action,build,database",
    [
        ("migrate", "candidate", "scratch"),
        ("migrate", "baseline", "scratch"),
        ("blocked-history", "candidate", "primary"),
        ("blocked-history", "candidate", "scratch"),
        ("blocked-history", "baseline", "primary"),
        ("blocked-migrate", "baseline", "scratch"),
        ("blocked-migrate", "candidate", "primary"),
    ],
)
def test_fixed_migration_actions_reject_other_epoch_database_pairs(action, build, database):
    with pytest.raises(renderer.RuntimeManifestError):
        render(
            "observer",
            action=action,
            build=build,
            database=database,
            job_name="observer",
            restore_point="blocked" if database == "scratch" else None,
        )


@pytest.mark.parametrize("build", ["baseline", "candidate"])
@pytest.mark.parametrize("database", ["primary", "scratch"])
def test_read_history_alone_selects_fixed_observer_settings(build, database):
    value = render(
        "observer",
        action="read-history",
        build=build,
        database=database,
        job_name="history-reader",
        restore_point="final" if database == "scratch" else None,
    )
    (container,) = value["spec"]["template"]["spec"]["containers"]
    assert container["command"] == ["python", "-m", "qualification.upgrade.runtime_steps"]
    assert container["args"] == ["read-history"]
    assert container["image"] == getattr(config(), build + "_image")
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["DJANGO_SETTINGS_MODULE"] == "qualification.upgrade.runtime_history_settings"
    assert env["DJANGO_RAY_UPGRADE_DATABASE"] == database
    assert env["DJANGO_RAY_UPGRADE_BUILD"] == build
    assert value["spec"]["backoffLimit"] == 0
    assert value["spec"]["activeDeadlineSeconds"] == 600
    assert "${" not in json.dumps(value)


def test_read_history_has_no_case_or_caller_supplied_module():
    options = {
        "action": "read-history",
        "build": "candidate",
        "database": "scratch",
        "job_name": "history-reader",
        "restore_point": "final",
    }
    with pytest.raises(renderer.RuntimeManifestError):
        render("observer", case="old-success", **options)
    with pytest.raises(TypeError):
        render("observer", settings_module="arbitrary.settings", **options)


@pytest.mark.parametrize("build", ["baseline", "candidate"])
@pytest.mark.parametrize("restore_point", ["blocked", "final", "rollback"])
@pytest.mark.parametrize(
    "case", ["old-success", "old-failure", "old-cancel", "old-retry", "old-gated"]
)
def test_scratch_inspection_selects_only_fixed_restored_old_artifacts(build, restore_point, case):
    value = render(
        "observer",
        action="inspect",
        case=case,
        build=build,
        database="scratch",
        job_name="scratch-reader",
        restore_point=restore_point,
    )
    pod = value["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert {row["name"]: row["value"] for row in container["env"]}[
        "DJANGO_RAY_UPGRADE_ARTIFACT_ROOT"
    ] == "/artifacts"
    assert [item for item in container["volumeMounts"] if item["name"] == "artifacts"] == [
        {
            "name": "artifacts",
            "mountPath": "/artifacts",
            "subPath": ".upgrade-restores/" + restore_point,
        }
    ]
    assert [item for item in pod["volumes"] if "persistentVolumeClaim" in item] == [
        {
            "name": "artifacts",
            "persistentVolumeClaim": {"claimName": "runtime-artifacts"},
        }
    ]
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": config().admitted_node}


@pytest.mark.parametrize("build", ["baseline", "candidate"])
@pytest.mark.parametrize("action", ["read-history", "compare-history"])
@pytest.mark.parametrize("restore_point", ["final", "rollback"])
def test_scratch_settled_history_uses_final_or_rollback_copy(build, action, restore_point):
    result = render(
        "observer",
        action=action,
        build=build,
        database="scratch",
        job_name="history-reader",
        restore_point=restore_point,
    )
    (container,) = result["spec"]["template"]["spec"]["containers"]
    assert container["args"] == [action]
    assert (
        next(item for item in container["volumeMounts"] if item["name"] == "artifacts")["subPath"]
        == ".upgrade-restores/" + restore_point
    )


@pytest.mark.parametrize(
    "action,case,build,database,restore_point",
    [
        ("blocked-history", None, "baseline", "scratch", None),
        ("blocked-history", None, "baseline", "scratch", "final"),
        ("blocked-history", None, "baseline", "scratch", "rollback"),
        ("blocked-migrate", None, "candidate", "scratch", None),
        ("blocked-migrate", None, "candidate", "scratch", "final"),
        ("blocked-migrate", None, "candidate", "scratch", "rollback"),
        ("read-history", None, "candidate", "scratch", None),
        ("read-history", None, "baseline", "scratch", "blocked"),
        ("compare-history", None, "candidate", "scratch", None),
        ("compare-history", None, "candidate", "scratch", "blocked"),
        ("inspect", "old-success", "baseline", "scratch", None),
        ("inspect", "current-core", "candidate", "scratch", "final"),
        ("inspect", "current-jobs", "candidate", "scratch", "rollback"),
        ("read-history", None, "candidate", "primary", "final"),
        ("inspect", "old-gated", "baseline", "primary", "blocked"),
        ("enqueue", "old-success", "baseline", "scratch", "final"),
        ("migrate", None, "candidate", "scratch", "final"),
    ],
)
def test_restore_matrix_refuses_before_loading_any_template(
    monkeypatch, action, case, build, database, restore_point
):
    monkeypatch.setattr(
        renderer, "_templates", lambda: pytest.fail("invalid restore selection reached rendering")
    )
    with pytest.raises(renderer.RuntimeManifestError):
        render(
            "observer",
            action=action,
            case=case,
            build=build,
            database=database,
            job_name="observer",
            restore_point=restore_point,
        )


@pytest.mark.parametrize(
    "point",
    [True, 1, [], {}, "", "../final", "/artifacts", ".upgrade-restores/final", "final/../blocked"],
)
def test_restore_point_is_an_exact_enum_not_a_path(monkeypatch, point):
    monkeypatch.setattr(
        renderer, "_templates", lambda: pytest.fail("invalid restore selection reached rendering")
    )
    with pytest.raises(renderer.RuntimeManifestError):
        render(
            "observer",
            action="read-history",
            build="candidate",
            database="scratch",
            job_name="observer",
            restore_point=point,
        )


@pytest.mark.parametrize(
    "template,options",
    [
        ("ray", {"build": "candidate"}),
        ("manager", {}),
        ("postgres", {}),
        ("artifact_pvc", {}),
    ],
)
def test_non_observer_roles_cannot_select_a_restore(template, options):
    with pytest.raises(renderer.RuntimeManifestError):
        render(template, restore_point="final", **options)


@pytest.mark.parametrize(
    "fault",
    ["subpath", "expression", "duplicate", "wrong-pvc", "alias-pvc", "nested-mount", "missing"],
)
def test_template_cannot_redirect_or_expose_another_artifact_root(tmp_path, monkeypatch, fault):
    data = yaml.safe_load(renderer._PROFILE.read_text())
    pod = data["templates"]["observer"]["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    mount = next(item for item in container["volumeMounts"] if item["name"] == "artifacts")
    volume = next(item for item in pod["volumes"] if item["name"] == "artifacts")
    if fault == "subpath":
        mount["subPath"] = "unreviewed"
    elif fault == "expression":
        mount["subPathExpr"] = "$(UNREVIEWED)"
    elif fault == "duplicate":
        container["volumeMounts"].append(dict(mount))
    elif fault == "wrong-pvc":
        volume["persistentVolumeClaim"]["claimName"] = "other-artifacts"
    elif fault == "alias-pvc":
        pod["volumes"].append(
            {"name": "original", "persistentVolumeClaim": {"claimName": "runtime-artifacts"}}
        )
        container["volumeMounts"].append({"name": "original", "mountPath": "/original-artifacts"})
    elif fault == "nested-mount":
        container["volumeMounts"].append({"name": "tmp", "mountPath": "/artifacts/inputs"})
    else:
        container["volumeMounts"].remove(mount)
    profile = tmp_path / "runtime.yaml"
    profile.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(renderer, "_PROFILE", profile)
    with pytest.raises(renderer.RuntimeManifestError):
        render(
            "observer",
            action="read-history",
            build="candidate",
            database="scratch",
            job_name="observer",
            restore_point="final",
        )


def test_scratch_render_does_not_change_primary_mounts_or_template_bytes():
    raw = renderer._PROFILE.read_bytes()
    render(
        "observer",
        action="read-history",
        build="candidate",
        database="scratch",
        job_name="observer",
        restore_point="rollback",
    )
    primary = [
        render(),
        render("ray", build="candidate"),
        render(
            "observer",
            action="read-history",
            build="candidate",
            database="primary",
            job_name="observer",
        ),
    ]

    def check(value):
        if type(value) is dict:
            if value.get("name") == "artifacts" and "mountPath" in value:
                assert value == {"name": "artifacts", "mountPath": "/artifacts"}
            for item in value.values():
                check(item)
        elif type(value) is list:
            for item in value:
                check(item)

    check(primary)
    assert renderer._PROFILE.read_bytes() == raw
