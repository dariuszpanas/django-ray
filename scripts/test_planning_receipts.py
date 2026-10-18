"""Fixed 45180ea qualification assertions; no artifact-supplied code imports.

These are retained-observation checks in addition to authenticated successful
source execution. They cannot reproduce discarded live state, database history,
image layers or namespace ownership from JSON alone.
"""

from __future__ import annotations

import hashlib
import re

from scripts.test_planning_results import (
    Cell,
    Evidence,
    ExpectedRun,
    NormalizationError,
    equal,
    require,
    sha,
)

IMAGE_ID = r"[a-zA-Z0-9./:_@-]{0,240}sha256:[0-9a-f]{64}"
IMAGE = r"[a-zA-Z0-9./:_-]{1,240}@sha256:[0-9a-f]{64}"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
MISSING_UPGRADE = [
    "real-old-manager-drain-and-uncertain-work-reconciliation",
    "unsupported-old-execution-refusal-and-carrier-retirement",
    "current-ray-core-and-jobs-smoke-and-manager-crash-recovery",
    "rendered-workflow-history-and-encrypted-runtimeenv-recovery",
    "migration-specific-code-rollback-after-candidate-writes",
]


def fields(value: object, expected: dict, code: str) -> None:
    if not isinstance(value, dict):
        raise NormalizationError(code)
    for key, item in expected.items():
        require(key in value, code)
        equal(value[key], item, code)


def source(evidence: Evidence, path: str, expected: ExpectedRun) -> None:
    equal(
        evidence.text(path).splitlines(),
        [expected.execution_commit, expected.execution_tree],
        "wrong-artifact-source",
    )


def image(value: object) -> None:
    require(isinstance(value, str) and re.fullmatch(IMAGE, value) is not None, "invalid-image")


def package(value: dict, *, module: str | None = None, version: str = "0.7.0") -> str:
    fields(
        value, {"distribution_name": "django-ray", "version": version}, "wrong-installed-package"
    )
    for key in ("wheel_sha256", "source_package_tree_sha256", "installed_package_tree_sha256"):
        require(sha(value[key]), "invalid-package-digest")
    equal(
        value["installed_package_tree_sha256"],
        value["source_package_tree_sha256"],
        "installed-source-package-mismatch",
    )
    location = value["module_location"]
    require(
        isinstance(location, str)
        and len(location) <= 512
        and location == value["distribution_location"] + "/django_ray/__init__.py",
        "invalid-installed-module",
    )
    if module is not None:
        equal(location, module, "wrong-installed-module")
    return location


def manifest(value: dict, schema: str) -> None:
    fields(
        value,
        {"schema": schema, "schema_version": 1, "outcome": "passed", "failure": None},
        "failed-execution-manifest",
    )


def application(evidence: Evidence, expected: ExpectedRun) -> None:
    from qualification.application.protected_logs import validate_receipt as validate_logs
    from qualification.application.resource_profiles import select_profile
    from qualification.application.run_chainsaw import RECEIPTS, parse_receipts
    from qualification.application.worker_images import validate_receipt as validate_workers
    from scripts.test_planning_application_receipts import (
        validate_application_generations,
        validate_application_payload,
    )

    root = f"application-core-{expected.run_id}/"
    build, output = root + "application-build/", root + "application-evidence/"
    source(evidence, build + "source.txt", expected)
    require(
        re.fullmatch(r"[0-9a-f]{64}  [^\r\n]{1,512}", evidence.text(build + "source.sha256"))
        is not None,
        "invalid-source-archive-digest",
    )
    equal(
        evidence.text(build + "resource-profile.txt"), "standard", "diagnostic-application-profile"
    )
    equal(
        evidence.text(build + "validation-intent.txt"),
        "acceptance",
        "diagnostic-application-profile",
    )
    inspected = evidence.json(build + "images.json")
    require(isinstance(inspected, list) and len(inspected) == 2, "invalid-application-images")
    images = {}
    for name, value in zip(("base", "candidate"), inspected, strict=True):
        require(
            isinstance(value["RepoDigests"], list) and len(value["RepoDigests"]) == 1,
            "invalid-application-images",
        )
        images[name] = value["RepoDigests"][0]
        image(images[name])
    layers = inspected[0]["RootFS"]["Layers"]
    require(
        isinstance(layers, list)
        and 0 < len(layers) <= 128
        and all(
            isinstance(layer, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", layer)
            for layer in layers
        )
        and inspected[1]["RootFS"]["Layers"][: len(layers)] == layers,
        "wrong-application-ancestry",
    )
    profile = select_profile("standard", "acceptance")
    summary = evidence.json(output + "summary.json")
    fields(
        summary,
        {
            "status": "passed",
            "namespace_removed": True,
            "source_tree": expected.execution_tree,
            "image": images["candidate"],
            "cold_ray": "required",
            "complete_application_gate": False,
            "qualification_profile": profile,
        },
        "incomplete-application-summary",
    )
    require(
        re.fullmatch(r"django-ray-core-[0-9a-f]{16}", summary["namespace"]) is not None
        and re.fullmatch(UUID, summary["namespace_uid"]) is not None,
        "invalid-application-namespace",
    )
    validate_workers(summary["worker_images"])
    for _container, names in RECEIPTS.values():
        # Parse each file strictly first; source parser validates the ordered batch.
        lines = []
        for name in names:
            validate_application_payload(name, evidence.json(output + name + ".json"))
            lines.append(evidence.read(output + name + ".json"))
        parse_receipts(b"\n".join(lines), names, expected_profile=profile)
    before = evidence.read(output + "before-nodes.json")
    fields(
        evidence.json(output + "after-nodes.json"),
        {
            "cold_replacement": True,
            "previous_receipt_sha256": hashlib.sha256(before).hexdigest(),
        },
        "unbound-cold-generation",
    )
    validate_application_generations(
        evidence.json(output + "before-nodes.json"),
        evidence.json(output + "after-nodes.json"),
        evidence.json(output + "setup.json"),
        before_core=evidence.json(output + "before-core.json"),
        after_core=evidence.json(output + "after-core.json"),
        before_workflows=evidence.json(output + "before-workflows.json"),
        after_workflows=evidence.json(output + "after-workflows.json"),
    )
    for generation in ("before", "after"):
        validate_logs(evidence.json(output + generation + "-protected-logs.json"), generation)
    producers = evidence.json(output + "producers.json")
    require(isinstance(producers, list) and len(producers) == 4, "missing-application-producers")
    require(len({value["uid"] for value in producers}) == 4, "duplicate-application-producer")
    equal(
        sorted(value["container"] for value in producers),
        ["assertions"] * 3 + ["setup"],
        "invalid-application-producer",
    )
    for (app, (container, _names)), value in zip(RECEIPTS.items(), producers, strict=True):
        require(
            set(value) == {"pod", "uid", "container", "image_id"}
            and isinstance(value["pod"], str)
            and re.fullmatch(app + r"-[a-z0-9-]{1,63}", value["pod"]) is not None
            and isinstance(value["uid"], str)
            and re.fullmatch(UUID, value["uid"]) is not None
            and value["container"] == container,
            "invalid-application-producer-identity",
        )
        require(
            re.fullmatch(IMAGE_ID, value["image_id"]) is not None
            and value["image_id"].removeprefix("containerd://").rsplit("@", 1)[-1]
            == images["candidate"].rsplit("@", 1)[-1],
            "wrong-application-producer-image",
        )


def _native_observations(name: str, backend: str, runner: str, crash: bool, value: dict) -> None:
    from qualification.upgrade.worker_modes import validate_receipt as validate_modes

    if name == "candidate-legacy-failure":
        equal(
            value,
            {
                "real_failed_jobs": 2,
                "real_succeeded_jobs": 2,
                "expired_malformed_outcome": "LOST",
                "expired_missing_outcome": "LOST",
                "fixture_effects": 4,
                "unknown_outcome": "LOST",
                "valid_completion_outcome": "SUCCEEDED",
                "automatic_retries": 0,
                "job_log_requests": 0,
                "history_preserved": True,
                "active_leases": 0,
                "ray_children_reaped": True,
            },
            "invalid-legacy-reconciliation",
        )
        return
    graph_refused = name == "baseline-post-write" and backend == "postgresql"
    fixed = {
        "historical_tasks": 4,
        "read_only_graph_lock_refused": graph_refused,
        "workflow_graphs_read": 0 if graph_refused else 2 if name == "baseline-post-write" else 1,
    }
    if runner == "ray_job":
        fixed.update(encrypted_runtime_env_preserved=True, missing_and_wrong_keys_rejected=True)
    if name == "baseline-post-write":
        fixed.update(post_write_tasks_read=9, database_read_only=True, migrations_retained=True)
    if name == "candidate-read":
        fixed["retired_carriers"] = {
            "legacy_runtime_env_refused": 4,
            "job_carriers_refused": 4,
            "inline_job_carriers_refused": 2,
            "core_carriers_refused": 2,
            "workflow_carriers_refused": 3,
            "malformed_job_refused": True,
            "cli_refusal_exit": 78,
            "application_boundary_calls": 0,
            "django_setup": False,
        }
    if name.endswith("-run"):
        candidate = name == "candidate-run"
        crash_facts = {}
        if candidate and crash:
            crash_facts = {"signal": "SIGKILL", "running_blocker_preserved": True}
            if runner == "ray_job":
                crash_facts.update(
                    owner_changed=True, same_job_attempt_generation=True, application_invocations=1
                )
            else:
                crash_facts.update(
                    old_ray_children_retired=True,
                    lost_without_automatic_retry=True,
                    explicit_retry_new_attempt_generation=True,
                    retry_then_claim_generation_advances=2,
                    application_invocations=2,
                )
        fixed.update(
            states={
                "success": "SUCCEEDED",
                "failure": "FAILED",
                "retry": "SUCCEEDED",
                "cancelled": "CANCELLED",
            },
            queued_blockers=4,
            running_blocker=True,
            manager_exit=143 if runner == "ray_job" else 130,
            active_leases=0,
            nonterminal=0,
            runner=runner,
            manager_crash=crash_facts,
            completion_encoding_failure={
                "state": "FAILED",
                "application_invocations": 1,
                "attempt_number": 1,
                "identity_bearing_job_completion": runner == "ray_job",
            }
            if candidate
            else None,
            worker_modes=None,
        )
        if candidate and runner == "ray_core" and not crash:
            validate_modes(value["worker_modes"])
            fixed["worker_modes"] = value["worker_modes"]
        if candidate and crash:
            # The public CAS command brackets stopped native fixture cohorts.
            # Source/job provenance establishes execution order; retain both
            # exact transitions and drain/preservation facts in the receipt.
            fixed["lease_policy"] = {
                name: {
                    "expected_seconds": previous_seconds,
                    "expected_revision": previous_revision,
                    "lease_seconds": seconds,
                    "revision": previous_revision + 1,
                    "history_preserved": True,
                    "leases_preserved": True,
                    "active_leases": 0,
                    "nonterminal": 0,
                }
                for name, previous_seconds, previous_revision, seconds in (
                    ("before_crash", 60, 1, 6),
                    ("after_drain", 6, 2, 60),
                )
            }
    equal(value, fixed, "invalid-native-phase-observations")


def native(evidence: Evidence, expected: ExpectedRun, variant: str) -> None:
    root = f"upgrade-native-qualification-{variant}/"
    source(evidence, root + "upgrade-source.txt", expected)
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", evidence.text(root + "upgrade-image.txt")) is not None,
        "invalid-native-image",
    )
    value = evidence.json(root + "upgrade-receipts/execution-manifest.json")
    manifest(value, "django-ray.coordinated-beta-native-upgrade")
    runner = "ray_job" if variant.startswith("jobs") else "ray_core"
    crash = variant in {"jobs-crash", "core-loss"}
    fields(
        value,
        {
            "runner": runner,
            "crash_manager": crash,
            "fixture_cleanup": True,
            "baseline_version": "0.6.0",
            "baseline_commit": "f22429c0d7eaabc7568de9031b3cc4f80e73fceb",
            "candidate_version": "0.7.0",
            "complete_upgrade_gate": False,
            "missing_acceptance": MISSING_UPGRADE,
        },
        "invalid-native-contract",
    )
    baseline = evidence.text(root + "upgrade-baseline-sha256.txt")
    require(
        sha(baseline) and sha(value["candidate_source_files_sha256"]),
        "invalid-native-source-digest",
    )
    equal(value["baseline_archive_sha256"], baseline, "wrong-released-archive")
    require(set(value["identities"]) == {"candidate", "released"}, "missing-installed-identities")
    modules = {}
    for name, version in (("candidate", "0.7.0"), ("released", "0.6.0")):
        modules[name] = package(value["identities"][name]["package"], version=version)
        require(
            re.fullmatch(
                r"/tmp/native-upgrade-[A-Za-z0-9_-]{1,64}/" + name + r"/django_ray/__init__\.py",
                modules[name],
            )
            is not None,
            "invalid-native-install-root",
        )
    require(
        modules["candidate"].split("/candidate/")[0] == modules["released"].split("/released/")[0],
        "mismatched-native-install-roots",
    )
    backends = value["backends"]
    require(isinstance(backends, list) and len(backends) == 2, "missing-native-backends")
    phases = [
        "baseline-run",
        "baseline-read",
        "candidate-read",
        "candidate-run",
        "baseline-post-write",
    ]
    if runner == "ray_job":
        phases.append("candidate-legacy-failure")
    for backend, receipt in zip(("sqlite", "postgresql"), backends, strict=True):
        fields(receipt, {"backend": backend, "fixture_cleanup": True}, "invalid-native-backend")
        require(
            sha(receipt["backup_sha256"]) and sha(receipt["artifacts_sha256"]),
            "invalid-native-backup",
        )
        equal([phase["phase"] for phase in receipt["phases"]], phases, "incomplete-native-phases")
        for name, phase in zip(phases, receipt["phases"], strict=True):
            require(
                set(phase) == {"phase", "version", "module", "observations"}, "invalid-native-phase"
            )
            candidate = name.startswith("candidate-")
            fields(
                phase,
                {
                    "version": "0.7.0" if candidate else "0.6.0",
                    "module": modules["candidate" if candidate else "released"],
                },
                "wrong-native-phase-install",
            )
            _native_observations(name, backend, runner, crash, phase["observations"])


def latency(evidence: Evidence, expected: ExpectedRun) -> None:
    from qualification.latency.contract import validate_probe

    root = "job-latency-qualification/"
    source(evidence, root + "latency-source.txt", expected)
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", evidence.text(root + "latency-image.txt")) is not None,
        "invalid-latency-image",
    )
    value = evidence.json(root + "latency-receipts/execution-manifest.json")
    manifest(value, "django-ray.latency-qualification")
    fields(
        value,
        {
            "fixture_cleanup": True,
            "workload": "ray-job-completion-latency",
            "definition_path": "qualification/latency/jobs.yaml",
        },
        "invalid-latency-contract",
    )
    module = "/tmp/django-ray-latency-qualification/target/django_ray/__init__.py"
    package(value["candidate"], module=module)
    validate_probe(value["observations"], expected_module=module)


def cancellation(evidence: Evidence, expected: ExpectedRun) -> None:
    from qualification.cancellation.contract import CHILD_SECONDS, OUTER_SECONDS, validate_receipt

    root = "cancellation-qualification/"
    source(evidence, root + "cancellation-source.txt", expected)
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", evidence.text(root + "cancellation-image.txt"))
        is not None,
        "invalid-cancellation-image",
    )
    value = evidence.json(root + "cancellation-receipts/execution-manifest.json")
    manifest(value, "django-ray.cancellation-qualification")
    fields(
        value,
        {
            "scope": "owned-ray-job-driver",
            "cleanup_failed": False,
            "server_stopped": True,
            "fixture_removed": True,
            "child_budget_seconds": CHILD_SECONDS,
            "outer_budget_seconds": OUTER_SECONDS,
        },
        "invalid-cancellation-contract",
    )
    package(value["candidate"], module="/tmp/cancellation/target/django_ray/__init__.py")
    validate_receipt(value["observations"])


def monitoring(evidence: Evidence, expected: ExpectedRun) -> None:
    from scripts.local_kuberay_auth import validate_auth_receipt

    root = f"monitoring-qualification-{expected.run_id}/"
    build, output = root + "monitoring-build/", root + "monitoring-evidence/"
    source(evidence, build + "source.txt", expected)
    require(
        re.fullmatch(r"[0-9a-f]{64}  [^\r\n]{1,512}", evidence.text(build + "source.sha256"))
        is not None,
        "invalid-monitoring-archive-digest",
    )
    images = evidence.json(build + "images.json")
    require(
        set(images) == {"application", "postgres", "prometheus", "grafana", "ray", "importer"},
        "incomplete-monitoring-images",
    )
    for value in images.values():
        image(value)
    ancestry = evidence.json(build + "ancestry.json")
    image(ancestry["base"])
    fields(
        ancestry,
        {"base_layers_verified": True, "candidate": images["application"]},
        "wrong-monitoring-ancestry",
    )
    value = evidence.json(output + "summary.json")
    fields(
        value,
        {
            "status": "passed",
            "phase": "complete",
            "namespace_removed": True,
            "cold_ray": "skip",
            "complete_application_gate": False,
            "private_surfaces": True,
            "source_head": expected.execution_commit,
            "source_tree": expected.execution_tree,
        },
        "invalid-monitoring-summary",
    )
    require(sha(value["rendered_config_sha256"]), "invalid-rendered-config-digest")
    equal(
        value["ray"],
        {
            "alive_nodes": 2,
            "logical_cpus": 2,
            "version": "2.58.0",
            "default_dashboard_uid": "rayDefaultDashboard",
            "panel_id": 41,
            "source_panel_type": "graph",
        },
        "invalid-monitoring-ray",
    )
    for name in ("http", "after_replay"):
        observed = value[name]
        fields(
            observed,
            {
                "status": "passed",
                "targets": {"django-ray": 1, "ray-head": 1, "ray-workers": 1},
                "metrics_auth": "isolated",
                "grafana_auth": "401/401/200",
                "dashboards": ["ray-cluster", "rayDefaultDashboard"],
                "datasource_query": "passed",
                "probe_host": "django-ray.localhost",
                "probe_paths": ["/api/health", "/api/readyz", "/api/livez"],
            },
            "incomplete-monitoring-http",
        )
        for key in ("prometheus_version", "grafana_version"):
            require(
                re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]{0,24})?", observed[key])
                is not None,
                "invalid-monitoring-version",
            )
    equal(
        value["replacement"],
        {"status": "passed", "provisioned_restored": True, "imported_absent": True},
        "invalid-dashboard-replacement",
    )
    validate_auth_receipt(value["ray_auth"], surface="ray")
    browser = value["browser"]
    fields(
        browser,
        {
            "status": "passed",
            "dashboard_uid": "rayDefaultDashboard",
            "panel_id": 41,
            "panel_title": "Cluster Utilization",
            "visible_canvas": True,
            "cpu_limit_millicores": 250,
            "memory_limit_mib": 512,
            "pids_limit": 256,
            "oom_killed": False,
            "cleanup_confirmed": True,
        },
        "invalid-browser-observation",
    )
    require(
        type(browser["numeric_query_values"]) is int and browser["numeric_query_values"] > 0,
        "missing-rendered-query-data",
    )
    components = {
        "django-web": "application",
        "postgres": "postgres",
        "prometheus": "prometheus",
        "grafana": "grafana",
        "ray-head": "ray",
        "ray-worker": "ray",
        "dashboard-importer": "importer",
    }
    containers = value["containers"]
    require(
        isinstance(containers, list)
        and len(containers) == 7
        and {item["component"] for item in containers} == set(components),
        "incomplete-live-container-census",
    )
    for container in containers:
        fields(
            container,
            {"image": images[components[container["component"]]], "ready": True, "restarts": 0},
            "invalid-live-container",
        )
        require(re.fullmatch(IMAGE_ID, container["image_id"]) is not None, "missing-live-image-id")
    resources = value["live_resources"]
    fields(resources, {"pods": 6}, "invalid-live-resources")
    for key, maximum in (("cpu_millicores", 2750), ("memory_mib", 8832)):
        require(
            type(resources[key]) is int and 0 < resources[key] <= maximum, "invalid-live-resources"
        )
    operator = value["operator"]
    fields(
        operator,
        {
            "image": "quay.io/kuberay/operator:v1.6.2@sha256:2602383ef61afe1b4c55bdcb24da2ff55ee31b6674cbe53b1583e5219e5e3376",
            "ready": True,
            "restarts": 0,
            "version": "1.6.2",
            "cpu_millicores": 100,
            "memory_mib": 512,
        },
        "invalid-operator",
    )
    require(re.fullmatch(IMAGE_ID, operator["image_id"]) is not None, "missing-operator-image-id")
    fields(
        evidence.json(output + "infrastructure-cleanup.json"),
        {
            "status": "passed",
            "failed_components": [],
            "disposable_cluster_removed": True,
            "registry_removed": True,
            "browser_removed": True,
        },
        "incomplete-monitoring-infrastructure-cleanup",
    )


def validate_cell(evidence: Evidence, expected: ExpectedRun, cell: Cell) -> None:
    if cell.category == "native":
        native(evidence, expected, cell.variant)
    else:
        {
            "application": application,
            "latency": latency,
            "cancellation": cancellation,
            "monitoring": monitoring,
        }[cell.category](evidence, expected)
