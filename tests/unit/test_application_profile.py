"""Cross-resource contracts for the finite application-core workload.

Chainsaw owns schema validation. These source tests protect the assertion sequence,
credential ordering, shared paths and conservative inventory bounds.
"""

from pathlib import Path

import yaml

from qualification.application.run_chainsaw import CREDENTIAL_KEYS

ROOT = Path(__file__).resolve().parents[2]


def definition():
    return yaml.safe_load(
        (ROOT / "qualification/application/core.yaml").read_text(encoding="utf-8")
    )


def operations():
    return [entry for step in definition()["spec"]["steps"] for entry in step["try"]]


def resources():
    result = {}
    for operation in operations():
        if "create" not in operation:
            continue
        resource = operation["create"]["resource"]
        identity = resource["kind"], resource["metadata"]["name"]
        if identity in result:
            assert resource == result[identity], (
                "recreated resources must keep the same reviewed body"
            )
        result[identity] = resource
    return result


def templates(resource):
    if resource["kind"] in {"Deployment", "Job"}:
        return [(resource["spec"]["template"], 1)]
    if resource["kind"] == "RayCluster":
        spec = resource["spec"]
        return [(spec["headGroupSpec"]["template"], 1)] + [
            (group["template"], group["replicas"]) for group in spec["workerGroupSpecs"]
        ]
    return []


def test_profile_waits_for_assertions_around_identical_cold_replacement():
    sequence = operations()
    before = next(
        i
        for i, op in enumerate(sequence)
        if op.get("assert", {}).get("resource", {}).get("metadata", {}).get("name")
        == "assert-before"
    )
    deleted = next(
        i
        for i, op in enumerate(sequence)
        if op.get("delete", {}).get("ref", {}).get("kind") == "RayCluster"
    )
    assert sequence[before]["assert"]["resource"]["status"] == {"succeeded": 1}
    assert sequence[deleted]["delete"]["ref"] == {
        "apiVersion": "ray.io/v1",
        "kind": "RayCluster",
        "name": "ray",
    }
    recreated = sequence[deleted + 1]["create"]["resource"]
    assert recreated == resources()["RayCluster", "ray"]
    after = next(
        i
        for i, op in enumerate(sequence)
        if op.get("create", {}).get("resource", {}).get("metadata", {}).get("name")
        == "assert-after"
    )
    assert before < deleted < after
    assert sequence[-1]["assert"]["resource"]["metadata"]["name"] == "assert-after"
    assert sequence[-1]["assert"]["resource"]["status"] == {"succeeded": 1}


def test_profile_preserves_finite_serial_execution_and_assertion_commands():
    test = definition()
    assert test["apiVersion"] == "chainsaw.kyverno.io/v1alpha1"
    assert test["kind"] == "Test"
    assert len(test["spec"]["steps"]) == 16
    for generation in ("before", "after"):
        job = resources()["Job", f"assert-{generation}"]
        assert job["spec"]["parallelism"] == job["spec"]["completions"] == 1
        assert job["spec"]["backoffLimit"] == 0
        assert job["spec"]["activeDeadlineSeconds"] == 600
        command = job["spec"]["template"]["spec"]["containers"][0]["command"]
        assert command[:2] == ["/bin/sh", "-ec"]
        commands = command[2].split(" && ")
        node_command, core_command = commands[:2]
        assert len(commands) == (3 if generation == "before" else 2)
        if generation == "before":
            assert (
                commands[2]
                == "python -m qualification.application.retire_manager --before-core /receipts/before-core.json --receipt /receipts/before-retirement.json"
            )
        assert ("--previous-retirement /receipts/before-retirement.json" in core_command) == (
            generation == "after"
        )
        assert node_command.startswith("python -m qualification.application.generic_nodes ")
        assert core_command.startswith("python -m qualification.application.run_core ")
        assert f"--receipt /receipts/{generation}-nodes.json" in node_command
        assert f"--receipt /receipts/{generation}-core.json" in core_command
        assert ("--previous-receipt /receipts/before-nodes.json" in node_command) == (
            generation == "after"
        )


def test_profile_uses_public_foreground_cleanup_configuration():
    config = yaml.safe_load((ROOT / "qualification/application/chainsaw.yaml").read_text())
    assert config["spec"]["cleanup"] == {"skipDelete": True}
    assert config["spec"]["execution"] == {"failFast": True, "parallel": 1}
    assert config["spec"]["deletion"] == {"propagation": "Foreground"}
    assert (
        next(op["delete"] for op in operations() if "delete" in op)["deletionPropagationPolicy"]
        == "Foreground"
    )
    for resource in resources().values():
        if resource["kind"] == "PersistentVolumeClaim":
            assert resource["spec"]["storageClassName"] == "($values.storageClass)"


def test_profile_bounds_source_inventory_including_init():
    # Compare a conservative sum of unique resources, not just concurrent Jobs.
    cpu, memory, scratch, pods, storage = 0, 0, 0, 0, 0

    def mib(value):
        assert value.endswith(("Mi", "Gi"))
        return int(value[:-2]) * (1024 if value.endswith("Gi") else 1)

    for resource in resources().values():
        if resource["kind"] == "PersistentVolumeClaim":
            storage += mib(resource["spec"]["resources"]["requests"]["storage"])
        for template, replicas in templates(resource):
            spec = template["spec"]
            regular = [c["resources"]["limits"] for c in spec["containers"]]
            init = [c["resources"]["limits"] for c in spec.get("initContainers", [])]
            for dimension in ("cpu", "memory", "ephemeral-storage"):
                convert = (
                    (lambda value: int(value.removesuffix("m"))) if dimension == "cpu" else mib
                )
                peak = max(
                    [
                        sum(convert(item[dimension]) for item in regular),
                        *(convert(item[dimension]) for item in init),
                    ]
                )
                if dimension == "cpu":
                    cpu += replicas * peak
                elif dimension == "memory":
                    memory += replicas * peak
                else:
                    scratch += replicas * peak
            pods += replicas
    assert (cpu, memory, scratch, pods, storage) == (2600, 9728, 3200, 7, 1408)
    assert cpu <= 4000 and memory <= 10240 and scratch <= 8192 and pods <= 8 and storage <= 16384


def test_profile_credentials_precede_the_composed_secret():
    keys = set(CREDENTIAL_KEYS)
    for resource in resources().values():
        for template, _ in templates(resource):
            spec = template["spec"]
            for container in spec["containers"] + spec.get("initContainers", []):
                if container["name"] == "postgres":
                    continue
                env = container["env"]
                names = [item["name"] for item in env]
                for key in ("DJANGO_SECRET_KEY_PART_ONE", "DJANGO_SECRET_KEY_PART_TWO"):
                    assert key in keys
                    assert names.index(key) < names.index("DJANGO_SECRET_KEY")
                    assert env[names.index(key)]["valueFrom"]["secretKeyRef"]["key"] == key
                assert (
                    env[names.index("DJANGO_SECRET_KEY")]["value"]
                    == "$(DJANGO_SECRET_KEY_PART_ONE)$(DJANGO_SECRET_KEY_PART_TWO)"
                )


def test_profile_mounts_same_archive_read_only_outside_setup():
    inventory = resources()
    config = inventory["ConfigMap", "application-config"]["data"]
    assert config["DJANGO_RAY_RECOVERY_WORKING_DIR"] == "/runtime/recovery.zip"
    web = inventory["Deployment", "django-web"]["spec"]["template"]["spec"]
    setup = web["initContainers"][0]
    assert setup["command"][-2:] == ["--receipt", "/receipts/setup.json"]
    assert any(
        mount == {"name": "receipts", "mountPath": "/receipts", "readOnly": False}
        for mount in setup["volumeMounts"]
    )
    assert not any(mount["name"] == "receipts" for mount in web["containers"][0]["volumeMounts"])
    for resource in inventory.values():
        for template, _ in templates(resource):
            spec = template["spec"]
            assert spec["automountServiceAccountToken"] is False
            assert spec["securityContext"]["runAsNonRoot"] is True
            for container in spec["containers"] + spec.get("initContainers", []):
                assert container["securityContext"]["readOnlyRootFilesystem"] is True
                assert container["securityContext"]["allowPrivilegeEscalation"] is False
                assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
                if container["name"] != "postgres":
                    runtime = next(
                        mount for mount in container["volumeMounts"] if mount["name"] == "runtime"
                    )
                    assert runtime["mountPath"] == "/runtime"
                    assert runtime["readOnly"] is (container["name"] != "setup")
            for volume in spec["volumes"]:
                assert not set(volume) & {"hostPath", "projected"}


def test_profile_uses_fixed_generic_ray_and_postgresql_images():
    for resource in resources().values():
        assert "namespace" not in resource["metadata"]
        for template, _ in templates(resource):
            for container in template["spec"]["containers"] + template["spec"].get(
                "initContainers", []
            ):
                image = container["image"]
                assert image == "($values.applicationImage)" or "@sha256:" in image
                if container["name"].startswith("ray-"):
                    assert image.startswith("rayproject/ray@sha256:")
    for resource in resources().values():
        if resource["kind"] == "Service":
            assert resource["spec"]["type"] == "ClusterIP"
            assert resource["spec"]["clusterIP"] == "None"
            assert not set(resource["spec"]) & {"externalIPs", "externalName"}


def test_manager_is_finite_retired_and_reaped_before_ray_replacement():
    sequence = operations()
    manager = resources()["Job", "django-manager"]
    assert manager["spec"]["backoffLimit"] == 0
    assert manager["spec"]["activeDeadlineSeconds"] == 1800
    assert manager["spec"]["parallelism"] == manager["spec"]["completions"] == 1
    assert manager["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    reaped = next(
        i
        for i, op in enumerate(sequence)
        if op.get("delete", {}).get("ref", {}).get("kind") == "Job"
    )
    assert sequence[reaped - 1]["assert"]["resource"]["status"] == {"succeeded": 1}
    assert sequence[reaped]["delete"]["ref"]["name"] == "django-manager"
    assert sequence[reaped]["delete"]["deletionPropagationPolicy"] == "Foreground"
    assert sequence[reaped + 1]["delete"]["ref"]["kind"] == "RayCluster"
    creates = [
        i
        for i, op in enumerate(sequence)
        if op.get("create", {}).get("resource", {}).get("metadata", {}).get("name")
        == "django-manager"
    ]
    assert len(creates) == 2 and creates[0] < reaped < creates[1]
