"""Finite declarations never become per-task environment eligibility keys."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from django_ray.runner import cohort_configuration as configuration
from django_ray.target.attestation import RayRunnerFamily
from django_ray.target.cohort_intent import CohortSelectionPolicy


def inputs(*, jobs=False):
    return {
        "tasks": {
            "first": {"OPTIONS": {"RAY_ADDRESS": "http://first:8265"}, "QUEUES": ["shared"]},
            "second": {"OPTIONS": {"RAY_ADDRESS": "http://second:8265"}, "QUEUES": ["shared"]},
            "other": {"OPTIONS": {}, "QUEUES": ["other"]},
        },
        "validated_aliases": ("first", "second", "other"),
        "selected_queues": ("shared",),
        "manager_settings": {
            "RAY_ADDRESS": "auto",
            "RUNTIME_ENV_PROFILES": {"project": {"env_vars": {"MARKER": "trusted"}}},
            "DEFAULT_RUNTIME_ENV_PROFILE": "project",
        },
        "django_settings_module": "project.settings",
        "runner_family": RayRunnerFamily.RAY_JOB if jobs else RayRunnerFamily.RAY_CORE,
        "execution_mode": "ray" if jobs else "cluster",
        "core_address": None if jobs else "ray://selected:10001",
        "core_control_settings": {},
    }


def test_core_has_one_connection_identity_and_separate_current_alias_declarations():
    selected = inputs()
    first = configuration.prepare_cohort_worker_configuration(**selected)
    assert tuple(item.alias for item in first.aliases) == ("first", "second")
    assert first.aliases[0].declaration_digest != first.aliases[1].declaration_digest
    assert first.control_runtime_env_json is first.django_settings_module is None
    assert first.job_addresses == ()
    assert all(
        item.control_profile_digest == first.core_configuration_digest for item in first.aliases
    )
    selected["tasks"]["first"]["OPTIONS"]["RAY_ADDRESS"] = "http://replacement:8265"
    changed = configuration.prepare_cohort_worker_configuration(**selected)
    assert changed.core_configuration_digest == first.core_configuration_digest
    assert changed.aliases[0].declaration_digest != first.aliases[0].declaration_digest
    assert changed.aliases[1] == first.aliases[1]


def test_jobs_retains_only_selected_addresses_from_the_same_declaration_snapshot():
    selected = inputs(jobs=True)
    selected["tasks"]["second"]["OPTIONS"].pop("RAY_ADDRESS")
    selected["manager_settings"]["RAY_ADDRESS"] = "ray://manager:10001"
    plan = configuration.prepare_cohort_worker_configuration(**selected)
    assert plan.job_addresses == (
        ("first", "http://first:8265"),
        ("second", "ray://manager:10001"),
    )
    selected["tasks"]["first"]["OPTIONS"]["RAY_ADDRESS"] = "http://changed:8265"
    changed = configuration.prepare_cohort_worker_configuration(**selected)
    assert plan.job_addresses[0] == ("first", "http://first:8265")
    assert changed.job_addresses[0] == ("first", "http://changed:8265")
    assert changed.aliases[0].declaration_digest != plan.aliases[0].declaration_digest
    assert changed.aliases[1] == plan.aliases[1]
    assert "http://first:8265" not in repr(plan)
    assert "ray://manager:10001" not in repr(plan)


def test_core_task_environment_changes_do_not_become_eligibility_keys():
    selected = inputs()
    original = configuration.prepare_cohort_worker_configuration(**selected)
    selected["manager_settings"]["DEFAULT_RUNTIME_ENV_PROFILE"] = "missing-task-profile"
    selected["tasks"]["first"]["OPTIONS"]["RAY_RUNTIME_ENV"] = {"invalid": object()}
    selected["django_settings_module"] = None
    assert configuration.prepare_cohort_worker_configuration(**selected) == original


def test_core_physical_connection_and_used_control_options_change_its_descriptor():
    selected = inputs()
    first = configuration.prepare_cohort_worker_configuration(**selected)
    selected["core_control_settings"] = {"namespace": "second"}
    second = configuration.prepare_cohort_worker_configuration(**selected)
    assert first.core_configuration_digest != second.core_configuration_digest
    assert first.aliases[0].declaration_digest == second.aliases[0].declaration_digest
    selected["core_address"] = "ray://replacement:10001"
    assert configuration.prepare_cohort_worker_configuration(
        **selected
    ).core_configuration_digest != (second.core_configuration_digest)


def test_local_descriptor_does_not_adopt_a_backend_endpoint():
    selected = inputs()
    selected.update(execution_mode="local", core_address=None)
    first = configuration.prepare_cohort_worker_configuration(**selected)
    selected["tasks"]["first"]["OPTIONS"]["RAY_ADDRESS"] = "http://elsewhere:8265"
    second = configuration.prepare_cohort_worker_configuration(**selected)
    assert first.core_configuration_digest == second.core_configuration_digest


def test_jobs_uses_only_fixed_manager_profile_and_pins_its_settings_module():
    selected = inputs(jobs=True)
    selected["tasks"]["first"]["OPTIONS"].update(
        RUNTIME_ENV_PROFILE="untrusted-task-profile", RAY_RUNTIME_ENV={"poison": object()}
    )
    original = deepcopy(selected["manager_settings"])
    plan = configuration.prepare_cohort_worker_configuration(**selected)
    assert plan.core_configuration_digest is None
    assert json.loads(plan.control_runtime_env_json) == {
        "env_vars": {"MARKER": "trusted", "DJANGO_SETTINGS_MODULE": "project.settings"}
    }
    assert selected["manager_settings"] == original
    assert "trusted" not in repr(plan)
    selected["manager_settings"]["RUNTIME_ENV_PROFILES"]["project"]["env_vars"]["MARKER"] = "new"
    changed = configuration.prepare_cohort_worker_configuration(**selected)
    assert changed.control_profile_digest != plan.control_profile_digest
    assert changed.aliases[0].declaration_digest == plan.aliases[0].declaration_digest


def test_jobs_default_inline_manager_profile_remains_supported():
    selected = inputs(jobs=True)
    selected["manager_settings"] = {"RAY_ADDRESS": "auto", "RAY_RUNTIME_ENV": {"pip": ["a==1"]}}
    plan = configuration.prepare_cohort_worker_configuration(**selected)
    assert json.loads(plan.control_runtime_env_json)["pip"] == ["a==1"]


def test_jobs_null_environment_variables_accept_pinned_settings():
    selected = inputs(jobs=True)
    selected["manager_settings"] = {"RAY_RUNTIME_ENV": {"env_vars": None}}
    plan = configuration.prepare_cohort_worker_configuration(**selected)
    assert json.loads(plan.control_runtime_env_json) == {
        "env_vars": {"DJANGO_SETTINGS_MODULE": "project.settings"}
    }


def test_owned_profile_upload_binds_configuration_to_different_submitted_mapping(monkeypatch):
    from django_ray.runner import cohort_job_control
    from django_ray.runner.cohort_qualification import CohortQualificationLifecycle
    from django_ray.runtime.cohort_job import probe_job_request_digest
    from django_ray.runtime.cohort_job_entrypoint import CohortProbeJobLaunch
    from tests.unit.test_cohort_job import NOW, request

    selected = inputs(jobs=True)
    selected["manager_settings"] = {"RAY_RUNTIME_ENV": {"working_dir": "private-source"}}
    plan = configuration.prepare_cohort_worker_configuration(**selected)
    alias = plan.aliases[0]
    requested = replace(request(), configuration_digest=alias.declaration_digest)
    uploaded = []

    def upload(spec):
        uploaded.append(deepcopy(spec))
        spec["working_dir"] = "gcs://_ray_pkg_0123456789abcdef.zip"

    monkeypatch.setattr(cohort_job_control, "_runtime", lambda *_args: None)
    monkeypatch.setattr(
        cohort_job_control,
        "_client",
        lambda *_args, **_kwargs: SimpleNamespace(
            _upload_working_dir_if_needed=upload, _upload_py_modules_if_needed=lambda _spec: None
        ),
    )
    result = cohort_job_control.execute_cohort_job_control(
        "prepare",
        {
            "ray_address": "http://first:8265",
            "control_runtime_env_json": plan.control_runtime_env_json,
            "source_control_profile_digest": plan.control_profile_digest,
            "django_settings_module": plan.django_settings_module,
            "expected_package_version": requested.expected_package_version,
            "expected_runtime": asdict(requested.expected_runtime),
        },
    )
    assert uploaded == [json.loads(plan.control_runtime_env_json)]
    assert result["source_control_profile_digest"] == alias.control_profile_digest
    assert result["submitted_runtime_env_digest"] != alias.control_profile_digest
    owner = CohortQualificationLifecycle(
        lease=requested.lease,
        package_version=requested.expected_package_version,
        runtime=requested.expected_runtime,
        runner_family=RayRunnerFamily.RAY_JOB,
        monotonic=lambda: 10.0,
        wall_clock=lambda: NOW + timedelta(seconds=1),
    )
    owner.configure_aliases(plan.aliases)
    launch = CohortProbeJobLaunch(
        requested,
        probe_job_request_digest(requested),
        result["jobs_endpoint"],
        result["submitted_runtime_env_digest"],
        plan.django_settings_module,
    )
    ticket = owner.begin_job(
        alias.alias,
        launch,
        source_control_profile_digest=result["source_control_profile_digest"],
        timeout_seconds=30,
        now=NOW + timedelta(seconds=1),
    )
    assert owner.outstanding is ticket
    assert json.loads(plan.control_runtime_env_json)["working_dir"] == "private-source"


def test_queue_names_keep_exact_unicode_and_spaces_with_ordered_deduplication():
    selected = inputs()
    names = ("email urgent", "通知", " exact ", "email urgent")
    selected["tasks"]["first"]["QUEUES"] = names
    selected["selected_queues"] = names
    plan = configuration.prepare_cohort_worker_configuration(**selected)
    assert plan.aliases[0].queues == names[:3]


@pytest.mark.parametrize("queue", [" ", "\x00", "x" * 101, 1])
def test_invalid_queues_are_refused_without_exposing_values(queue):
    selected = inputs()
    selected["selected_queues"] = [queue]
    with pytest.raises(configuration.CohortConfigurationError):
        configuration.prepare_cohort_worker_configuration(**selected)


@pytest.mark.parametrize("jobs", [False, True])
def test_jobs_only_alias_is_never_admitted_to_core(jobs):
    selected = inputs(jobs=jobs)
    selected["tasks"]["first"]["OPTIONS"]["RAY_JOB_ONLY"] = True
    plan = configuration.prepare_cohort_worker_configuration(**selected)
    assert tuple(item.alias for item in plan.aliases) == (
        ("first", "second") if jobs else ("second",)
    )
    if jobs:
        assert plan.aliases[0].selection_policy is CohortSelectionPolicy.JOBS_ONLY


def test_default_address_and_normalized_trust_remain_declaration_inputs():
    selected = inputs(jobs=True)
    selected["tasks"]["first"]["OPTIONS"] = {}
    selected["manager_settings"]["WORKFLOW_PLAN_TRUST_IDENTITY"] = {"trust_domain": "one"}
    first = configuration.prepare_cohort_worker_configuration(**selected)
    selected["manager_settings"]["RAY_ADDRESS"] = "http://fallback:8265"
    second = configuration.prepare_cohort_worker_configuration(**selected)
    assert first.aliases[0].declaration_digest != second.aliases[0].declaration_digest
    assert first.aliases[1].declaration_digest == second.aliases[1].declaration_digest
    selected["manager_settings"]["WORKFLOW_PLAN_TRUST_IDENTITY"] = {"trust_domain": "two"}
    third = configuration.prepare_cohort_worker_configuration(**selected)
    assert second.aliases[0].declaration_digest != third.aliases[0].declaration_digest
    assert second.aliases[1].declaration_digest != third.aliases[1].declaration_digest


def test_declared_queue_sets_keep_worker_selection_order():
    selected = inputs()
    selected["tasks"]["first"]["QUEUES"] = {"shared", "last"}
    selected["selected_queues"] = ("last", "shared")
    plan = configuration.prepare_cohort_worker_configuration(**selected)
    assert plan.aliases[0].queues == ("last", "shared")


@pytest.mark.parametrize(
    "change",
    [
        {"validated_aliases": ("first", "first")},
        {"validated_aliases": tuple(f"alias{i}" for i in range(65))},
        {"validated_aliases": ("missing",)},
        {"selected_queues": ()},
        {"selected_queues": "shared"},
        {"runner_family": "ray_job"},
        {"django_settings_module": "invalid/module"},
        {"manager_settings": {"DEFAULT_RUNTIME_ENV_PROFILE": "missing"}},
        {"execution_mode": "sync"},
    ],
)
def test_invalid_jobs_configuration_is_fixed_and_redacted(change):
    selected = inputs(jobs=True) | change
    with pytest.raises(configuration.CohortConfigurationError) as error:
        configuration.prepare_cohort_worker_configuration(**selected)
    assert str(error.value) == "Invalid current-cohort manager configuration"
    assert error.value.__suppress_context__


def test_oversized_control_profile_is_refused_without_restricting_task_profiles():
    selected = inputs(jobs=True)
    selected["manager_settings"] = {"RAY_RUNTIME_ENV": {"env_vars": {"SECRET": "x" * 65537}}}
    with pytest.raises(configuration.CohortConfigurationError):
        configuration.prepare_cohort_worker_configuration(**selected)


@pytest.mark.parametrize(
    "change",
    [
        {"execution_mode": "sync"},
        {"execution_mode": "local", "address": "http://endpoint"},
        {"address": "http://user:secret@host"},
        {"control_settings": False},
    ],
)
def test_invalid_core_connection_descriptor_is_refused(change):
    selected = {"execution_mode": "cluster", "address": "ray://head:10001", "control_settings": {}}
    with pytest.raises(configuration.CohortConfigurationError):
        configuration.cohort_core_connection_digest(**(selected | change))
