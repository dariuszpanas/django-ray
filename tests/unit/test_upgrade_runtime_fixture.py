"""Static image/Pod ownership checks; these do not qualify a native upgrade."""

from __future__ import annotations

import ast
import hashlib
import io
import re
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "qualification/upgrade/RuntimeDockerfile"
PROFILE = ROOT / "qualification/upgrade/runtime.yaml"


def test_database_clients_use_debian_versioned_binary_directory():
    from qualification.upgrade.runtime_database import POSTGRES_BIN

    assert POSTGRES_BIN.as_posix() == "/usr/lib/postgresql/17/bin"
    recipe = DOCKERFILE.read_text(encoding="utf-8")
    assert "for client in psql pg_dump pg_restore createdb dropdb; do" in recipe
    assert '/usr/lib/postgresql/17/bin/"$client" --version || exit 1;' in recipe


def definitions():
    return yaml.safe_load(PROFILE.read_text(encoding="utf-8"))


def pod_specs():
    templates = definitions()["templates"]
    return {
        "postgres": templates["postgres"]["spec"]["template"]["spec"],
        "head": templates["ray"]["spec"]["headGroupSpec"]["template"]["spec"],
        "worker": templates["ray"]["spec"]["workerGroupSpecs"][0]["template"]["spec"],
        "manager": templates["manager"]["spec"]["template"]["spec"],
        "observer": templates["observer"]["spec"]["template"]["spec"],
    }


def script(marker):
    text = DOCKERFILE.read_text(encoding="utf-8")
    return text.split(f"<<'{marker}'\n", 1)[1].split(f"\n{marker}\n", 1)[0]


def build_functions(marker):
    tree = ast.parse(script(marker))
    tree.body = [
        node
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom | ast.FunctionDef)
    ]
    namespace = {}
    exec(compile(tree, str(DOCKERFILE), "exec"), namespace)
    return namespace


def test_templates_have_finite_roles_and_no_embedded_orchestration():
    data = definitions()
    assert set(data) == {"schema", "requirements", "templates"}
    assert data["schema"] == 1
    assert data["requirements"] == {
        "maxLiveRayGenerations": 1,
        "maxLiveManagers": 1,
        "maxLiveObservers": 1,
        "maxAdmittedPodPids": 1024,
        "completeUpgradeGate": False,
    }
    assert set(data["templates"]) == {
        "postgres_pvc",
        "artifact_pvc",
        "postgres_service",
        "postgres",
        "ray_service",
        "ray",
        "manager",
        "observer",
    }
    assert {row["kind"] for row in data["templates"].values()} == {
        "PersistentVolumeClaim",
        "Service",
        "Deployment",
        "RayCluster",
        "Job",
    }
    assert all(row["metadata"]["namespace"] == "${namespace}" for row in data["templates"].values())
    assert "initContainers" not in PROFILE.read_text()
    assert "migrate" not in str(data)


@pytest.mark.parametrize("role", ["postgres", "head", "worker", "manager", "observer"])
def test_pods_pin_rwo_node_and_mount_fixed_secret_files(role):
    pod = pod_specs()[role]
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "${admittedNode}"}
    assert pod["automountServiceAccountToken"] is False
    assert pod["terminationGracePeriodSeconds"] == 30
    assert "hostNetwork" not in pod and "hostPID" not in pod and "hostPath" not in str(pod)
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    secret = next(volume["secret"] for volume in pod["volumes"] if volume["name"] == "credentials")
    assert secret == {
        "secretName": "runtime-credentials",
        "defaultMode": 0o440,
        "items": [
            {"key": name, "path": name}
            for name in ("postgres-password", "runtime-env-key", "django-secret-key")
        ],
    }
    (container,) = pod["containers"]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert {
        "name": "credentials",
        "mountPath": "/run/upgrade-secrets",
        "readOnly": True,
    } in container["volumeMounts"]
    assert "envFrom" not in container
    values = {row["name"]: row["value"] for row in container["env"]}
    if role == "postgres":
        assert values["POSTGRES_PASSWORD_FILE"] == "/run/upgrade-secrets/postgres-password"
        assert "POSTGRES_PASSWORD" not in values
    else:
        assert container["image"] == "${epochImage}"
        assert values["DJANGO_SETTINGS_MODULE"] == "${settingsModule}"
        assert (
            values["DJANGO_RAY_UPGRADE_POSTGRES_PASSWORD_FILE"]
            == "/run/upgrade-secrets/postgres-password"
        )
        assert (
            values["DJANGO_RAY_UPGRADE_ENCRYPTION_KEY_FILE"]
            == "/run/upgrade-secrets/runtime-env-key"
        )
        assert values["DJANGO_SECRET_KEY_FILE"] == "/run/upgrade-secrets/django-secret-key"
        assert (
            not {
                "RAY_ADDRESS",
                "RAY_JOB_CONFIG_JSON_ENV_VAR",
                "DJANGO_SECRET_KEY",
                "DJANGO_RAY_UPGRADE_ENCRYPTION_KEY",
                "DJANGO_RAY_UPGRADE_POSTGRES_PASSWORD",
            }
            & values.keys()
        )


def test_fixed_resource_envelope_and_one_artifact_store():
    expected = {
        "postgres": {"cpu": "200m", "memory": "512Mi", "ephemeral-storage": "128Mi"},
        "head": {"cpu": "750m", "memory": "5120Mi", "ephemeral-storage": "1Gi"},
        "worker": {"cpu": "750m", "memory": "1536Mi", "ephemeral-storage": "1Gi"},
        "manager": {"cpu": "250m", "memory": "768Mi", "ephemeral-storage": "256Mi"},
        "observer": {"cpu": "200m", "memory": "512Mi", "ephemeral-storage": "256Mi"},
    }
    for role, pod in pod_specs().items():
        assert pod["containers"][0]["resources"] == {
            "requests": expected[role],
            "limits": expected[role],
        }
        for volume in pod["volumes"]:
            if "emptyDir" in volume:
                assert "sizeLimit" in volume["emptyDir"]
        if role != "postgres":
            assert {
                "name": "artifacts",
                "persistentVolumeClaim": {"claimName": "runtime-artifacts"},
            } in pod["volumes"]
    templates = definitions()["templates"]
    for name in ("postgres_pvc", "artifact_pvc"):
        assert templates[name]["spec"] == {
            "storageClassName": "${storageClass}",
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
        }


def test_ray_and_manager_have_no_replicated_or_retry_fallback():
    templates = definitions()["templates"]
    ray = templates["ray"]["spec"]
    assert ray["rayVersion"] == "${epochRayVersion}"
    assert ray["enableInTreeAutoscaling"] is False
    (worker,) = ray["workerGroupSpecs"]
    assert (worker["replicas"], worker["minReplicas"], worker["maxReplicas"]) == (1, 1, 1)
    for group in (ray["headGroupSpec"], worker):
        assert group["rayStartParams"]["num-cpus"] == "1"
        assert group["rayStartParams"]["num-gpus"] == "0"
        assert group["rayStartParams"]["object-store-memory"] == "134217728"
    for role, deadline, command in (
        ("manager", 1800, ["python", "-m", "django", "django_ray_worker"]),
        ("observer", 600, ["python", "-m", "qualification.upgrade.runtime_steps"]),
    ):
        job = templates[role]["spec"]
        assert (job["backoffLimit"], job["parallelism"], job["completions"]) == (0, 1, 1)
        assert job["activeDeadlineSeconds"] == deadline
        assert job["template"]["spec"]["restartPolicy"] == "Never"
        container = job["template"]["spec"]["containers"][0]
        assert container["command"] == command
        assert container["args"] == "${" + role + "Args}"


def test_source_templates_expose_only_the_existing_artifact_pvc_root():
    # A fixed scratch subPath is selected by the renderer, never supplied as a
    # YAML input or inherited by the manager/Ray aliases sharing this mount.
    for role, pod in pod_specs().items():
        if role == "postgres":
            continue
        assert [volume for volume in pod["volumes"] if "persistentVolumeClaim" in volume] == [
            {
                "name": "artifacts",
                "persistentVolumeClaim": {"claimName": "runtime-artifacts"},
            }
        ]
        assert [
            mount for mount in pod["containers"][0]["volumeMounts"] if mount["name"] == "artifacts"
        ] == [
            {
                "name": "artifacts",
                "mountPath": "/artifacts",
            }
        ]


def test_placeholder_inventory_is_explicit_and_credentials_are_not_inputs():
    assert set(re.findall(r"\$\{(\w+)\}", PROFILE.read_text())) == {
        "namespace",
        "storageClass",
        "admittedNode",
        "postgresImage",
        "postgresUser",
        "primaryDatabase",
        "scratchDatabase",
        "runtimeBuild",
        "epochRayVersion",
        "epochImage",
        "epochPythonVersion",
        "database",
        "jobName",
        "managerArgs",
        "observerArgs",
        "settingsModule",
    }


@pytest.mark.parametrize(
    "fault", [None, "commit", "digest", "traversal", "symlink", "oversized-member"]
)
def test_build_archive_refuses_wrong_identity_or_unsafe_contents(tmp_path, fault):
    functions = build_functions("VERIFY_ARCHIVES")
    commit = "a" * 40
    archive_path = tmp_path / "source.tar"
    with tarfile.open(
        archive_path, "w", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}
    ) as archive:
        member = tarfile.TarInfo("../escaped" if fault == "traversal" else "pyproject.toml")
        if fault == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "../escaped"
            archive.addfile(member)
        else:
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if fault == "oversized-member":
        # No giant allocation: metadata bound is checked before extraction.
        original = functions["tarfile"].open

        class Oversized:
            def __enter__(self):
                self.archive = original(archive_path)
                self.pax_headers = self.archive.pax_headers
                return self

            def __exit__(self, *args):
                self.archive.close()

            def getmembers(self):
                members = self.archive.getmembers()
                members[0].size = 256 * 1024 * 1024 + 1
                return members

        functions["tarfile"] = type("FakeTar", (), {"open": staticmethod(lambda path: Oversized())})
    arguments = (
        archive_path,
        tmp_path / "output",
        "b" * 40 if fault == "commit" else commit,
        "b" * 64 if fault == "digest" else digest,
    )
    if fault is None:
        functions["unpack"](*arguments)
        assert (tmp_path / "output/pyproject.toml").read_bytes() == b"x"
    else:
        with pytest.raises(SystemExit, match="runtime-image-source-refused"):
            functions["unpack"](*arguments)
        assert not (tmp_path / "output").exists()
    assert not (tmp_path / "escaped").exists()


def test_installed_tree_comparison_detects_bytes_and_names(tmp_path):
    digest = build_functions("VERIFY_INSTALL")["package_digest"]
    source, installed = tmp_path / "source", tmp_path / "installed"
    for root in (source, installed):
        root.mkdir()
        (root / "__init__.py").write_bytes(b"version = '0.4.0'\n")
    assert digest(source) == digest(installed)
    (installed / "__init__.py").write_bytes(b"version = '0.5.0'\n")
    assert digest(source) != digest(installed)
    (installed / "__init__.py").write_bytes((source / "__init__.py").read_bytes())
    (installed / "extra.py").write_bytes(b"")
    assert digest(source) != digest(installed)


def test_image_separates_epoch_wheel_from_curated_qualifier_overlay():
    text = DOCKERFILE.read_text()
    assert "ARG PYTHON_IMAGE\nFROM ${PYTHON_IMAGE}" in text
    assert "--frozen --no-install-project --no-dev --extra postgres" in text
    assert "--offline --no-index --no-deps" in text
    assert '"dir_info" not in direct' in text
    assert "95ee5dfe95b1c1bed95ff28c4fcb5fcdc491e485" in script("VERIFY_ARCHIVES")
    assert '("0.4.0", "2.56.0")' in text and '("0.5.0", "2.58.0")' in text
    runtime = text.split("FROM ${PYTHON_IMAGE} AS runtime\n", 1)[1]
    assert "PYTHONPATH=/opt/qualifier" in runtime
    assert "/build/epoch" not in runtime and "COPY src" not in runtime
    assert "USER 1000:1000" in runtime
    assert "postgresql-client-17=17.11-0+deb13u1" in runtime
    overlay = re.findall(r"^COPY --from=builder /build/qualifier/(\S+) ", runtime, re.MULTILINE)
    assert set(overlay) == {
        "qualification/__init__.py",
        "qualification/docker/__init__.py",
        "qualification/docker/scenario.py",
        "qualification/upgrade/__init__.py",
        "qualification/upgrade/contract.py",
        "qualification/upgrade/runtime_contract.py",
        "qualification/upgrade/runtime_tasks.py",
        "qualification/upgrade/runtime_settings.py",
        "qualification/upgrade/runtime_steps.py",
        "qualification/upgrade/runtime_history.py",
        "qualification/upgrade/runtime_history_settings.py",
        "qualification/upgrade/runtime_artifacts.py",
        "qualification/upgrade/runtime_database.py",
        "qualification/upgrade/runtime_restore.py",
        "qualification/upgrade/runtime_scratch.py",
    }
    assert "ray.init" not in text and "django setup" not in text and "migrate" not in runtime
