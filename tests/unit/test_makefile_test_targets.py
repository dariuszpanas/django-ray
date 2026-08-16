from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import local_ci_runner
from scripts import local_resource_coordinator as coordinator

ROOT = Path(__file__).parents[2]
MAKEFILE = ROOT / "Makefile"


def _target_body(makefile: str, target: str) -> str:
    body = makefile.split(f"{target}:\n", maxsplit=1)[1]
    return body.split("\n# ", maxsplit=1)[0]


def _write_zero_exit_fake_make(tmp_path: Path) -> Path:
    if os.name == "nt":
        fake_make = tmp_path / "zero exit make.cmd"
        fake_make.write_text("@exit /b 0\n", encoding="utf-8")
    else:
        fake_make = tmp_path / "zero exit make"
        fake_make.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_make.chmod(0o755)
    return fake_make


def test_xdist_target_parallelizes_only_ordinary_local_tests() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    target = _target_body(makefile, "test-xdist")
    phony_targets = next(
        line for line in makefile.splitlines() if line.startswith(".PHONY:")
    ).split()

    assert "TEST_XDIST_WORKERS ?= 4" in makefile
    assert "test-xdist" in phony_targets
    assert target.strip("\n").splitlines() == [
        "\tpytest -n $(TEST_XDIST_WORKERS) --max-worker-restart=0 \\",
        '\t\t-m "not real_ray and not live_cluster and not postgresql"',
    ]
    assert "--dist" not in target


def test_ci_target_remains_serial_and_independent_of_local_xdist() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    target = _target_body(makefile, "ci") + _target_body(makefile, "_ci-owned")

    assert "test-xdist" not in target
    assert "pytest -n" not in target
    assert "--execution xdist" not in target


def test_ci_target_has_one_root_wrapper_and_a_guarded_non_nested_body() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    public = _target_body(makefile, "ci")
    owned = _target_body(makefile, "_ci-owned")

    assert public.lstrip().startswith("python -m scripts.local_resource_coordinator run")
    assert public.count("python -m scripts.local_resource_coordinator run") == 1
    assert "--profile ci-final" in public
    assert "--phase ci" in public
    assert "-- python -m scripts.local_ci_runner" in public
    assert "--make" not in public
    assert "$(MAKE)" not in public
    assert "uv run" not in public
    assert owned.lstrip().startswith(
        "python -m scripts.local_resource_coordinator require-inherited"
    )
    assert "--profile ci-final" in owned
    assert owned.index("require-inherited") < owned.index("ruff format --check")
    assert "\tmake --no-print-directory --jobs=1 test-testproject" in owned
    assert "MAKE_COMMAND" not in makefile
    assert "$(MAKE)" not in makefile
    assert "uv run" not in owned
    assert "python -m pytest" not in owned


def test_ci_rejects_make_ignore_errors_when_either_guarded_target_is_reached() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    guard = "GNU Make ignore-errors (-i/--ignore-errors) is forbidden for ci and _ci-owned"
    reserved_goal_guard = "$(error GNU Make MAKECMDGOALS is reserved and must not be assigned)"
    makeflags_guard = (
        "_ci-reject-makeflags-origin:\n"
        "\t$(error command-line or override MAKEFLAGS is forbidden for ci and _ci-owned)"
    )
    mflags_guard = (
        "_ci-reject-mflags-origin:\n"
        "\t$(error command-line or override MFLAGS is forbidden for ci and _ci-owned)"
    )
    ignore_guard = f"_ci-reject-ignore-errors:\n\t$(error {guard})"

    assert "override .DEFAULT_GOAL := all" in makefile
    assert "ifeq ($(filter default undefined,$(origin MAKECMDGOALS)),)" in makefile
    assert reserved_goal_guard in makefile
    assert "ifneq ($(filter command line override,$(origin MAKEFLAGS)),)" in makefile
    assert "ci _ci-owned: _ci-reject-makeflags-origin" in makefile
    assert "ifneq ($(filter command line override,$(origin MFLAGS)),)" in makefile
    assert "ci _ci-owned: _ci-reject-mflags-origin" in makefile
    assert (
        "override _DJANGO_RAY_MAKEFLAGS_FIRST_WORD := $(firstword $(value MAKEFLAGS))" in makefile
    )
    assert "override _DJANGO_RAY_MAKEFLAGS_SHORT :=" in makefile
    assert "override _DJANGO_RAY_MFLAGS_FIRST_WORD := $(firstword $(value MFLAGS))" in makefile
    assert "override _DJANGO_RAY_MFLAGS_SHORT :=" in makefile
    assert "override _DJANGO_RAY_IGNORE_ERRORS :=" in makefile
    assert "$(findstring i,$(_DJANGO_RAY_MAKEFLAGS_SHORT))" in makefile
    assert "$(findstring i,$(_DJANGO_RAY_MFLAGS_SHORT))" in makefile
    assert "$(filter --ignore-errors,$(value MAKEFLAGS) $(value MFLAGS))" in makefile
    assert "ifneq ($(_DJANGO_RAY_IGNORE_ERRORS),)" in makefile
    assert "ci _ci-owned: _ci-reject-ignore-errors" in makefile
    assert makeflags_guard in makefile
    assert mflags_guard in makefile
    assert ignore_guard in makefile
    assert (
        ".PHONY: _ci-reject-makeflags-origin _ci-reject-mflags-origin _ci-reject-ignore-errors"
    ) in makefile
    assert ".SECONDEXPANSION:" not in makefile
    assert "ci _ci-owned: $$(error" not in makefile
    assert "_DJANGO_RAY_CI_MAKE_GUARD" not in makefile
    assert makefile.index("override .DEFAULT_GOAL := all") < makefile.index(reserved_goal_guard)
    assert makefile.index(reserved_goal_guard) < makefile.index(
        "ifneq ($(filter command line override,$(origin MAKEFLAGS)),)"
    )
    assert makefile.index(
        "ifneq ($(filter command line override,$(origin MFLAGS)),)"
    ) < makefile.index("override _DJANGO_RAY_MAKEFLAGS_FIRST_WORD :=")
    assert makefile.index(ignore_guard) < makefile.index("\nci:\n")


@pytest.mark.parametrize("target", ("ci", "_ci-owned"))
@pytest.mark.parametrize(
    "ignore_option",
    ("-i", "--ignore-errors", "-ki"),
)
def test_ci_make_ignore_errors_never_reaches_any_recipe(
    target: str,
    ignore_option: str,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the guarded CI targets")

    result = subprocess.run(
        [make, "--no-print-directory", ignore_option, target],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "ignore-errors (-i/--ignore-errors) is forbidden" in result.stderr
    assert "python -m scripts.local_resource_coordinator" not in result.stderr
    assert "require-inherited" not in result.stderr
    assert "ruff format --check" not in result.stderr
    assert "All CI checks passed!" not in result.stderr


@pytest.mark.parametrize("target", ("ci", "_ci-owned"))
def test_ci_ignore_errors_guard_cannot_be_disabled_by_command_line_variables(
    target: str,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the guarded CI targets")

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "-i",
            target,
            "_DJANGO_RAY_MAKEFLAGS_ORIGIN=default",
            "_DJANGO_RAY_MFLAGS_ORIGIN=default",
            "_DJANGO_RAY_MAKEFLAGS_FIRST_WORD=",
            "_DJANGO_RAY_MAKEFLAGS_SHORT=",
            "_DJANGO_RAY_MFLAGS_FIRST_WORD=",
            "_DJANGO_RAY_MFLAGS_SHORT=",
            "_DJANGO_RAY_IGNORE_ERRORS=",
            "_DJANGO_RAY_CI_MAKE_GUARD=",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "ignore-errors (-i/--ignore-errors) is forbidden" in result.stderr
    assert "python -m scripts.local_resource_coordinator" not in result.stderr
    assert "require-inherited" not in result.stderr
    assert "ruff format --check" not in result.stderr


@pytest.mark.parametrize("target", ("ci", "_ci-owned"))
def test_ci_ignore_errors_guard_cannot_be_shadowed_by_target_specific_eval(
    target: str,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the guarded CI targets")

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "-i",
            "--dry-run",
            f"--eval={target}: override _DJANGO_RAY_IGNORE_ERRORS :=",
            f"--eval={target}: override _DJANGO_RAY_CI_MAKE_GUARD :=",
            target,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "ignore-errors (-i/--ignore-errors) is forbidden" in result.stderr
    assert "python -m scripts.local_resource_coordinator" not in result.stderr
    assert "require-inherited" not in result.stderr
    assert "ruff format --check" not in result.stderr


@pytest.mark.parametrize("target", ("ci", "_ci-owned"))
@pytest.mark.parametrize("flag_variable", ("MAKEFLAGS", "MFLAGS"))
def test_ci_rejects_command_line_flag_variable_origins_before_any_recipe_command(
    target: str,
    flag_variable: str,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the guarded CI targets")

    result = subprocess.run(
        [make, "--no-print-directory", target, f"{flag_variable}="],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert f"command-line or override {flag_variable} is forbidden" in result.stderr
    assert "python -m scripts.local_resource_coordinator" not in result.stderr
    assert "require-inherited" not in result.stderr
    assert "ruff format --check" not in result.stderr


@pytest.mark.parametrize("target", ("ci", "_ci-owned"))
def test_ci_ignore_errors_fails_closed_when_both_flag_variables_are_cleared(
    target: str,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the guarded CI targets")

    result = subprocess.run(
        [make, "--no-print-directory", "-i", target, "MAKEFLAGS=", "MFLAGS="],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "command-line or override MAKEFLAGS is forbidden" in result.stderr
    assert "python -m scripts.local_resource_coordinator" not in result.stderr
    assert "require-inherited" not in result.stderr
    assert "ruff format --check" not in result.stderr


@pytest.mark.parametrize("target", ("ci", "_ci-owned"))
@pytest.mark.parametrize("inherited_flags", ("-i", "--ignore-errors", "-ki"))
def test_inherited_make_ignore_errors_never_reaches_any_ci_recipe(
    target: str,
    inherited_flags: str,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the guarded CI targets")
    environment = os.environ.copy()
    environment["MAKEFLAGS"] = inherited_flags

    result = subprocess.run(
        [make, "--no-print-directory", target],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "ignore-errors (-i/--ignore-errors) is forbidden" in result.stderr
    assert "python -m scripts.local_resource_coordinator" not in result.stderr
    assert "require-inherited" not in result.stderr
    assert "ruff format --check" not in result.stderr
    assert "All CI checks passed!" not in result.stderr


def test_make_ignore_errors_remains_available_to_unrelated_targets() -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise an unrelated target")

    result = subprocess.run(
        [make, "--no-print-directory", "-i", "--dry-run", "help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Django-Ray Development Commands" in result.stdout
    assert "ignore-errors (-i/--ignore-errors) is forbidden" not in result.stderr


@pytest.mark.parametrize("guarded_target", ("ci", "_ci-owned"))
def test_make_eval_alias_cannot_bypass_ci_ignore_errors_guard(
    guarded_target: str,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise an evaluated alias")

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "-i",
            "--dry-run",
            f"--eval=alias-ci: {guarded_target}",
            "--eval=alias-ci: override _DJANGO_RAY_IGNORE_ERRORS :=",
            f"--eval={guarded_target}: override _DJANGO_RAY_IGNORE_ERRORS :=",
            "--eval=_ci-reject-ignore-errors: ; @echo injected-ignore-guard",
            "alias-ci",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "ignore-errors (-i/--ignore-errors) is forbidden" in result.stderr
    assert "python -m scripts.local_resource_coordinator" not in result.stderr
    assert "require-inherited" not in result.stderr
    assert "ruff format --check" not in result.stderr
    assert "injected-ignore-guard" not in result.stdout


@pytest.mark.parametrize("guarded_target", ("ci", "_ci-owned"))
def test_makefiles_alias_cannot_bypass_ci_ignore_errors_guard(
    guarded_target: str,
    tmp_path: Path,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise an imported alias")
    injected = tmp_path / "ci-alias.mk"
    injected.write_text(
        f"alias-ci: {guarded_target}\n"
        "alias-ci: override _DJANGO_RAY_IGNORE_ERRORS :=\n"
        f"{guarded_target}: override _DJANGO_RAY_IGNORE_ERRORS :=\n"
        "_ci-reject-ignore-errors: ; @echo injected-ignore-guard\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["MAKEFILES"] = str(injected)

    result = subprocess.run(
        [make, "--no-print-directory", "-i", "--dry-run", "alias-ci"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "ignore-errors (-i/--ignore-errors) is forbidden" in result.stderr
    assert "python -m scripts.local_resource_coordinator" not in result.stderr
    assert "require-inherited" not in result.stderr
    assert "ruff format --check" not in result.stderr
    assert "injected-ignore-guard" not in result.stdout


@pytest.mark.parametrize("ignore_option", (None, "-i"))
def test_no_goal_invocation_uses_ordinary_all_default(
    ignore_option: str | None,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository default goal")
    command = [make, "--no-print-directory", "--dry-run"]
    if ignore_option is not None:
        command.append(ignore_option)

    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "ruff format --check ." in result.stdout
    assert "python -m scripts.local_resource_coordinator" not in result.stdout
    assert "require-inherited" not in result.stdout
    assert "ignore-errors (-i/--ignore-errors) is forbidden" not in result.stderr


@pytest.mark.parametrize("spoof_origin", ("command-line", "environment"))
def test_makecmdgoals_spoof_is_rejected_before_unrelated_recipe_expansion(
    spoof_origin: str,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise MAKECMDGOALS origin checks")
    environment = os.environ.copy()
    command = [make, "--no-print-directory", "help"]
    if spoof_origin == "command-line":
        command.append("MAKECMDGOALS=ci")
    else:
        environment["MAKECMDGOALS"] = "ci"

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "MAKECMDGOALS is reserved and must not be assigned" in result.stderr
    assert "Django-Ray Development Commands" not in result.stderr


def test_makefiles_cannot_override_makecmdgoals_scope(
    tmp_path: Path,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise MAKEFILES origin checks")
    injected = tmp_path / "injected.mk"
    injected.write_text("override MAKECMDGOALS := help\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["MAKEFILES"] = str(injected)

    result = subprocess.run(
        [make, "--no-print-directory", "help"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "MAKECMDGOALS is reserved and must not be assigned" in result.stderr


@pytest.mark.parametrize("injection", ("command-line", "environment", "eval"))
def test_default_goal_injection_cannot_route_no_goal_invocation_into_ci(
    injection: str,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository default goal")
    environment = os.environ.copy()
    command = [make, "--no-print-directory", "--dry-run"]
    if injection == "command-line":
        command.append(".DEFAULT_GOAL=ci")
    elif injection == "environment":
        environment[".DEFAULT_GOAL"] = "ci"
    else:
        command.append("--eval=override .DEFAULT_GOAL := ci")

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "ruff format --check ." in result.stdout
    assert "python -m scripts.local_resource_coordinator" not in result.stdout
    assert "require-inherited" not in result.stdout
    assert "All CI checks passed!" not in result.stdout


def test_makefiles_cannot_redirect_the_repository_default_goal_to_ci(
    tmp_path: Path,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository default goal")
    injected = tmp_path / "default-goal-injected.mk"
    injected.write_text("override .DEFAULT_GOAL := ci\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["MAKEFILES"] = str(injected)

    result = subprocess.run(
        [make, "--no-print-directory", "--dry-run"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "ruff format --check ." in result.stdout
    assert "python -m scripts.local_resource_coordinator" not in result.stdout
    assert "require-inherited" not in result.stdout
    assert "All CI checks passed!" not in result.stdout


@pytest.mark.parametrize("flag_variable", ("MAKEFLAGS", "MFLAGS"))
def test_make_eval_cannot_override_ci_flag_origin_checks(flag_variable: str) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise Make eval origin checks")

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "-i",
            f"--eval=override {flag_variable} :=",
            f"--eval=_ci-reject-{flag_variable.lower()}-origin: ; @echo injected-origin-guard",
            "ci",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert f"command-line or override {flag_variable} is forbidden" in result.stderr
    assert "python -m scripts.local_resource_coordinator" not in result.stderr
    assert "injected-origin-guard" not in result.stdout


def test_ci_dry_run_never_executes_the_coordinator_or_private_body() -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository wrapper")
    environment = os.environ.copy()
    environment[coordinator.LOCAL_RESOURCE_RUN_ID_ENV] = "partial-inheritance-must-not-run"

    result = subprocess.run(
        [make, "--no-print-directory", "-j4", "--dry-run", "ci"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "python -m scripts.local_resource_coordinator run" in result.stdout
    assert "python -m scripts.local_ci_runner" in result.stdout
    assert "require-inherited" not in result.stdout
    assert "ruff format --check" not in result.stdout
    assert "jobserver unavailable" not in result.stderr.lower()


def test_private_ci_dry_run_prints_every_fixed_command_without_executing_guard() -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the private repository target")
    environment = os.environ.copy()
    environment[coordinator.LOCAL_RESOURCE_RUN_ID_ENV] = "partial-inheritance-must-not-run"

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "-j4",
            "--dry-run",
            "_ci-owned",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "require-inherited" in result.stdout
    assert "ruff format --check" in result.stdout
    assert "make --no-print-directory --jobs=1 test-testproject" in result.stdout
    assert "valid inherited CI ownership is required" not in result.stderr
    assert "jobserver unavailable" not in result.stderr.lower()


@pytest.mark.parametrize("target", ("ci", "_ci-owned"))
@pytest.mark.parametrize("override_source", ("command-line", "environment"))
def test_ci_make_executable_overrides_cannot_substitute_a_zero_exit_fake(
    target: str,
    override_source: str,
    tmp_path: Path,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository CI entrypoints")
    fake_make = _write_zero_exit_fake_make(tmp_path)
    environment = os.environ.copy()
    command = [make, "--no-print-directory", "--dry-run", target]
    if override_source == "command-line":
        command.extend((f"MAKE={fake_make}", f"MAKE_COMMAND={fake_make}"))
    else:
        environment.update({"MAKE": str(fake_make), "MAKE_COMMAND": str(fake_make)})

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert str(fake_make) not in result.stdout
    assert str(fake_make) not in result.stderr
    if target == "ci":
        assert "python -m scripts.local_resource_coordinator run" in result.stdout
        assert "-- python -m scripts.local_ci_runner" in result.stdout
        assert "--make" not in result.stdout
        assert "require-inherited" not in result.stdout
    else:
        assert "require-inherited" in result.stdout
        assert "make --no-print-directory --jobs=1 test-testproject" in result.stdout


def test_ci_entrypoint_scrubs_make_recursion_state_but_retains_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    assert {"MAKE", "MAKE_COMMAND", "MAKEFILES"} <= local_ci_runner._MAKE_ENVIRONMENT_KEYS
    for key in local_ci_runner._MAKE_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, "-j4 --jobserver-auth=3,4")
    for key in coordinator.LOCAL_RESOURCE_INHERITANCE_ENV_KEYS:
        monkeypatch.setenv(key, f"retained-{key}")
    monkeypatch.setattr(
        local_ci_runner,
        "require_inherited_local_resources",
        lambda **_kwargs: object(),
    )

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(local_ci_runner.subprocess, "run", run)

    assert local_ci_runner.main([]) == 0
    assert captured["command"] == [
        "make",
        "--no-print-directory",
        "-j1",
        "_ci-owned",
    ]
    assert captured["shell"] is False
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert all(key not in environment for key in local_ci_runner._MAKE_ENVIRONMENT_KEYS)
    assert all(
        environment[key] == f"retained-{key}"
        for key in coordinator.LOCAL_RESOURCE_INHERITANCE_ENV_KEYS
    )


def test_ci_entrypoint_replaces_ambient_git_config_with_exact_fsmonitor_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    hostile_git_config = {
        "GIT_CONFIG": "hostile-config-file",
        "GIT_CONFIG_SYSTEM": "hostile-system-config",
        "GIT_CONFIG_GLOBAL": "hostile-global-config",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_PARAMETERS": "'core.fsmonitor'='hostile-monitor'",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "hostile-monitor",
        "GIT_CONFIG_KEY_1": "include.path",
        "GIT_CONFIG_VALUE_1": "hostile-include",
        "GIT_CONFIG_KEY_999": "credential.helper",
        "GIT_CONFIG_VALUE_999": "hostile-helper",
        "Git_Config_Key_Not_An_Index": "core.hooksPath",
        "Git_Config_Value_Not_An_Index": "hostile-hooks",
    }
    for key, value in hostile_git_config.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        local_ci_runner,
        "require_inherited_local_resources",
        lambda **_kwargs: object(),
    )

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(local_ci_runner.subprocess, "run", run)

    assert local_ci_runner.main([]) == 0
    environment = captured["env"]
    assert isinstance(environment, dict)
    expected = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "false",
    }
    assert {
        key: value
        for key, value in environment.items()
        if key.upper()
        in {
            "GIT_CONFIG",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_SYSTEM",
        }
        or key.upper().startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    } == expected


def test_ci_entrypoint_rejects_unproved_inheritance_before_make_launch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def reject(**_kwargs: object) -> None:
        raise coordinator.LocalResourceInheritanceError("synthetic capability detail")

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Make must not launch without proved inheritance")

    monkeypatch.setattr(local_ci_runner, "require_inherited_local_resources", reject)
    monkeypatch.setattr(local_ci_runner.subprocess, "run", unexpected_run)

    assert local_ci_runner.main([]) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "valid inherited CI ownership is required" in captured.err
    assert "synthetic capability detail" not in captured.err


def test_ci_entrypoint_real_process_rejects_make_executable_override(
    tmp_path: Path,
) -> None:
    fake_make = _write_zero_exit_fake_make(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "scripts.local_ci_runner", "--make", str(fake_make)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "unrecognized arguments: --make" in result.stderr
    assert "All CI checks passed!" not in result.stderr


def test_local_resources_target_is_one_read_only_status_call() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    target = _target_body(makefile, "local-resources")

    assert "LOCAL_RESOURCES_FORMAT ?= text" in makefile
    assert target.strip() == (
        'python -m scripts.local_resource_coordinator status --format "$(LOCAL_RESOURCES_FORMAT)"'
    )
    assert "uv run" not in target


def test_local_resources_format_remains_one_quoted_cli_argument() -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository status target")

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "--dry-run",
            "local-resources",
            "LOCAL_RESOURCES_FORMAT=json text",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert shlex.split(result.stdout.strip())[-2:] == ["--format", "json text"]


def test_kuberay_status_requires_explicit_command_line_scope_and_is_read_only() -> None:
    makefile = (ROOT / "mk" / "k8s.mk").read_text(encoding="utf-8")
    target = _target_body(makefile, "k8s-final-gate-status")

    assert "$(origin K8S_CONTEXT)" in target
    assert "$(origin K8S_NAMESPACE)" in target
    assert target.strip().endswith("python -m scripts.local_kuberay_status")
    for public_name in (
        "K8S_CONTEXT",
        "K8S_NAMESPACE",
        "K8S_FINAL_GATE_STATUS_FORMAT",
    ):
        assert f"$({public_name})" not in target
        assert f"unexport {public_name}" in makefile
    private_mapping = {
        "K8S_CONTEXT": "DJANGO_RAY_INTERNAL_KUBERAY_CONTEXT",
        "K8S_NAMESPACE": "DJANGO_RAY_INTERNAL_KUBERAY_NAMESPACE",
        "K8S_FINAL_GATE_STATUS_FORMAT": "DJANGO_RAY_INTERNAL_KUBERAY_STATUS_FORMAT",
    }
    for public_name, private_name in private_mapping.items():
        assert f"{private_name} := $(value {public_name})" in makefile
    recipe_commands = [line.strip().lower() for line in target.splitlines()]
    assert not any(command.startswith("docker ") for command in recipe_commands)
    assert not any(command.startswith("kubectl ") for command in recipe_commands)


def test_kuberay_status_dry_run_never_executes_or_accepts_environment_scope() -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository status target")
    environment = os.environ.copy()
    environment.update(
        {
            "K8S_CONTEXT": "docker-desktop",
            "K8S_NAMESPACE": "django-ray",
            coordinator.LOCAL_RESOURCE_RUN_ID_ENV: "partial-inheritance-must-not-run",
        }
    )

    rejected = subprocess.run(
        [make, "--no-print-directory", "--dry-run", "k8s-final-gate-status"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    rejected_namespace = subprocess.run(
        [
            make,
            "--no-print-directory",
            "--dry-run",
            "k8s-final-gate-status",
            "K8S_CONTEXT=docker-desktop",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    accepted = subprocess.run(
        [
            make,
            "--no-print-directory",
            "--dry-run",
            "k8s-final-gate-status",
            "K8S_CONTEXT=docker-desktop",
            "K8S_NAMESPACE=django-ray",
            "K8S_FINAL_GATE_STATUS_FORMAT=json",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert rejected.returncode != 0
    assert "K8S_CONTEXT must be provided on the command line" in rejected.stderr
    assert "scripts.local_kuberay_status" not in rejected.stdout
    assert rejected_namespace.returncode != 0
    assert "K8S_NAMESPACE must be provided on the command line" in rejected_namespace.stderr
    assert "scripts.local_kuberay_status" not in rejected_namespace.stdout
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.count("python -m scripts.local_kuberay_status") == 1
    assert "docker-desktop" not in accepted.stdout
    assert "django-ray" not in accepted.stdout
    assert "json" not in accepted.stdout
    printed_commands = [line.strip().lower() for line in accepted.stdout.splitlines()]
    assert not any(command.startswith("kubectl ") for command in printed_commands)
    assert not any(command.startswith("docker ") for command in printed_commands)


@pytest.mark.parametrize(
    ("variable", "hostile"),
    (
        ("K8S_CONTEXT", 'docker-desktop" ; true #'),
        ("K8S_NAMESPACE", 'django-ray" && true #'),
        ("K8S_FINAL_GATE_STATUS_FORMAT", "$(error must-not-expand)"),
        ("K8S_FINAL_GATE_STATUS_FORMAT", '"unterminated'),
    ),
)
def test_kuberay_status_dry_run_never_interpolates_scope_or_format_data(
    variable: str,
    hostile: str,
) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository status target")
    values = {
        "K8S_CONTEXT": "docker-desktop",
        "K8S_NAMESPACE": "django-ray",
        "K8S_FINAL_GATE_STATUS_FORMAT": "text",
    }
    values[variable] = hostile

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "--dry-run",
            "k8s-final-gate-status",
            *(f"{key}={value}" for key, value in values.items()),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "python -m scripts.local_kuberay_status"
    assert hostile not in result.stdout
    assert hostile not in result.stderr


def test_postgres_target_includes_target_execution_evidence_migration() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    target = _target_body(makefile, "test-postgres")
    migration_test = "tests/integration/test_ray_task_target_execution_evidence_migration.py"

    assert target.count(migration_test) == 1
    assert target.index("test_ray_worker_target_capability_migration.py") < target.index(
        migration_test
    )
    assert "-m postgresql" in target
