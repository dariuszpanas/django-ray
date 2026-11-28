"""Workspace lease ownership, cleanup, and quarantine tests for topology preparation."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import django_ray.workflow.progress.preparation as preparation
from tests.unit._workflow_progress_preparation_helpers import (
    _partial_lease_workspace,
    _quarantined_workspace,
    _unleased_workspace,
)


@pytest.mark.parametrize("tampering", ["invalid-json", "wrong-workspace-id", "wrong-pid"])
def test_cleanup_refuses_unreadable_or_mismatched_owner_lease(
    tmp_path: Path,
    tampering: str,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    assert workspace.lease_path is not None
    original = workspace.lease_path.read_text(encoding="utf-8")
    if tampering == "invalid-json":
        workspace.lease_path.write_text("{", encoding="utf-8")
        message = "owner lease is invalid"
    elif tampering == "wrong-workspace-id":
        value = json.loads(original)
        value["workspace_id"] = "00000000-0000-0000-0000-000000000000"
        workspace.lease_path.write_text(json.dumps(value), encoding="utf-8")
        message = "lease UUID does not match"
    else:
        value = json.loads(original)
        value["pid"] = os.getpid() + 1
        workspace.lease_path.write_text(json.dumps(value), encoding="utf-8")
        message = "lease PID does not match"

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match=message,
    ):
        workspace.__exit__(None, None, None)

    assert workspace.cleanup_outcome == "refused"
    assert workspace.path_exists
    workspace.lease_path.write_text(original, encoding="utf-8")
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_cleanup_refuses_owner_lease_outside_the_control_reserve(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(
        preparation.SQLitePreparationConfig(control_reserve_bytes=4 * 1024),
        parent_directory=tmp_path,
    )
    workspace.__enter__()
    assert workspace.lease_path is not None
    original = workspace.lease_path.read_bytes()
    workspace.lease_path.write_bytes(b"x" * (4 * 1024 + 1))

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="owner lease exceeds the control reserve",
    ):
        workspace.__exit__(None, None, None)

    assert workspace.cleanup_outcome == "refused"
    assert workspace.path_exists
    workspace.lease_path.write_bytes(original)
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_cleanup_detects_workspace_reappearance_after_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    original_rmdir = Path.rmdir

    def remove_then_reappear(path: Path) -> None:
        original_rmdir(path)
        if path == workspace._cleanup_quarantine:
            directory.mkdir(mode=0o700)

    monkeypatch.setattr(Path, "rmdir", remove_then_reappear)
    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="reappeared during cleanup",
    ):
        workspace.__exit__(None, None, None)

    assert workspace.cleanup_outcome == "refused"
    assert directory.is_dir()
    directory.rmdir()
    preparation._discard_live_workspace(workspace)


def test_unleased_acquisition_detects_directory_reappearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path.resolve(strict=True)
    directory = parent / (preparation._WORKSPACE_PREFIX + "00000000-0000-0000-0000-000000000141")
    directory.mkdir(mode=0o700)
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=parent)
    workspace._creator_pid = os.getpid()
    workspace._creator_uid = os.geteuid() if os.name == "posix" else None
    workspace._owner_thread_id = threading.get_ident()
    workspace._owned_parent = parent
    workspace._owned_parent_identity = preparation._PathIdentity.from_stat(parent.stat())
    workspace._owned_directory = directory
    workspace._owned_directory_identity = preparation._PathIdentity.from_stat(directory.stat())
    original_rmdir = Path.rmdir

    def remove_then_reappear(path: Path) -> None:
        original_rmdir(path)
        if path == workspace._cleanup_quarantine:
            directory.mkdir(mode=0o700)

    monkeypatch.setattr(Path, "rmdir", remove_then_reappear)
    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="workspace reappeared",
    ):
        workspace._remove_empty_acquisition_directory()

    assert workspace.cleanup_outcome == "refused"
    assert directory.is_dir()
    directory.rmdir()


def test_unleased_cleanup_classifies_owned_path_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, directory = _unleased_workspace(tmp_path)
    original_stat = preparation.os.stat
    secret_path = "customer-secret/unleased-owned-path"

    def fail_directory_stat(path: object, *args: Any, **kwargs: Any):
        if Path(path) == directory:
            raise OSError(secret_path)
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(preparation.os, "stat", fail_directory_stat)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="ownership inspection failed",
        ) as captured:
            workspace._remove_empty_acquisition_directory()

    assert workspace.cleanup_outcome == "operational_failure"
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize(
    ("phase", "message"),
    [
        ("inspection", "unleased-directory inspection failed"),
        ("removal", "unleased-directory removal failed"),
    ],
)
def test_unleased_cleanup_filesystem_failures_are_pathless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    message: str,
) -> None:
    workspace, directory = _unleased_workspace(tmp_path)
    secret_path = f"customer-secret/unleased-{phase}"
    original_iterdir = Path.iterdir
    original_rmdir = Path.rmdir

    def fail_iterdir(path: Path):
        if path == directory:
            raise OSError(secret_path)
        return original_iterdir(path)

    def fail_rmdir(path: Path) -> None:
        if path == directory or path == workspace._cleanup_quarantine:
            raise OSError(secret_path)
        original_rmdir(path)

    with monkeypatch.context() as scoped:
        if phase == "inspection":
            scoped.setattr(Path, "iterdir", fail_iterdir)
        else:
            scoped.setattr(Path, "rmdir", fail_rmdir)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match=message,
        ) as captured:
            workspace._remove_empty_acquisition_directory()

    assert workspace.cleanup_outcome == "operational_failure"
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize("failure_type", [OSError, RuntimeError])
def test_owned_path_canonical_inspection_error_is_pathless_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    secret_path = "customer-secret/canonical-workspace"
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    original_resolve = Path.resolve

    def fail_directory_resolve(path: Path, *args: Any, **kwargs: Any):
        if path == directory:
            raise failure_type(secret_path)
        return original_resolve(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "resolve", fail_directory_resolve)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="canonical-path inspection failed",
        ) as captured:
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "operational_failure"
    assert workspace.path_exists
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


@pytest.mark.parametrize("scenario", ["identity", "unsafe-parent"])
def test_owned_path_rejects_identity_or_parent_safety_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        if scenario == "identity":
            original_identity = workspace._owned_directory_identity
            assert original_identity is not None
            workspace._owned_directory_identity = replace(
                original_identity,
                inode=original_identity.inode + 1,
            )
            try:
                with pytest.raises(
                    preparation.WorkflowProgressPreparationCleanupRefusedError,
                    match="ownership identity changed",
                ):
                    workspace._validated_owned_path()
            finally:
                workspace._owned_directory_identity = original_identity
        else:

            def reject_parent(_parent_stat: os.stat_result) -> None:
                raise preparation.WorkflowProgressPreparationWorkspaceAcquisitionError(
                    "injected unsafe parent"
                )

            with monkeypatch.context() as scoped:
                scoped.setattr(
                    workspace,
                    "_validate_acquisition_parent_stat",
                    reject_parent,
                )
                with pytest.raises(
                    preparation.WorkflowProgressPreparationCleanupRefusedError,
                    match="parent became unsafe",
                ):
                    workspace._validated_owned_path()


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode contract")
def test_owned_path_and_lease_reject_posix_authority_drift(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        directory = workspace.directory
        lease_path = workspace.lease_path
        assert directory is not None
        assert lease_path is not None

        original_uid = workspace._creator_uid
        workspace._creator_uid = None
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationCleanupRefusedError,
                match="owner identity is incomplete",
            ):
                workspace._validated_owned_path()
        finally:
            workspace._creator_uid = original_uid

        directory.chmod(0o755)
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationCleanupRefusedError,
                match="workspace permissions became unsafe",
            ):
                workspace._validated_owned_path()
        finally:
            directory.chmod(0o700)

        lease_path.chmod(0o644)
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationCleanupRefusedError,
                match="owner lease permissions are unsafe",
            ):
                workspace._validate_owned_directory(acquisition_failure=False)
        finally:
            lease_path.chmod(0o600)

        assert workspace.connection is not None
        workspace.connection.close()
        workspace.connection = None
        quarantine = workspace._quarantine_owned_directory()
        quarantine.chmod(0o755)
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationCleanupRefusedError,
                match="quarantine permissions became unsafe",
            ):
                workspace._validated_cleanup_quarantine()
        finally:
            quarantine.chmod(0o700)


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("incomplete", "ownership identity is incomplete"),
        ("missing", "ownership path is missing"),
        ("redirected", "path was replaced or redirected"),
        ("wrong-parent", "path was replaced or redirected"),
        ("wrong-prefix", "name has no owned prefix"),
        ("noncanonical-uuid", "name has no canonical UUID"),
        ("invalid-uuid", "name has no canonical UUID"),
    ],
)
def test_owned_path_validation_rejects_unprovable_boundaries(
    tmp_path: Path,
    scenario: str,
    message: str,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace._creator_pid = os.getpid()
    workspace._creator_uid = os.geteuid() if os.name == "posix" else None
    workspace._owner_thread_id = threading.get_ident()
    cleanup_directory: Path | None = None
    cleanup_parent: Path | None = None
    if scenario == "missing":
        workspace._owned_parent = tmp_path.resolve(strict=True)
        workspace._owned_directory = tmp_path / "missing"
    elif scenario == "redirected":
        nested = tmp_path / "nested"
        nested.mkdir()
        workspace._owned_parent = nested
        workspace._owned_directory = nested / ".."
        cleanup_parent = nested
    elif scenario == "wrong-parent":
        cleanup_directory = tmp_path / (
            preparation._WORKSPACE_PREFIX + "00000000-0000-0000-0000-000000000141"
        )
        cleanup_directory.mkdir(mode=0o700)
        cleanup_parent = tmp_path / "other-parent"
        cleanup_parent.mkdir()
        workspace._owned_parent = cleanup_parent.resolve(strict=True)
        workspace._owned_directory = cleanup_directory.resolve(strict=True)
    elif scenario == "wrong-prefix":
        cleanup_directory = tmp_path / "not-owned"
        cleanup_directory.mkdir(mode=0o700)
        workspace._owned_parent = tmp_path.resolve(strict=True)
        workspace._owned_directory = cleanup_directory.resolve(strict=True)
    elif scenario == "noncanonical-uuid":
        cleanup_directory = tmp_path / (
            preparation._WORKSPACE_PREFIX + "00000000-0000-0000-0000-0000000000AA"
        )
        cleanup_directory.mkdir(mode=0o700)
        workspace._owned_parent = tmp_path.resolve(strict=True)
        workspace._owned_directory = cleanup_directory.resolve(strict=True)
    elif scenario == "invalid-uuid":
        cleanup_directory = tmp_path / (preparation._WORKSPACE_PREFIX + "not-a-uuid")
        cleanup_directory.mkdir(mode=0o700)
        workspace._owned_parent = tmp_path.resolve(strict=True)
        workspace._owned_directory = cleanup_directory.resolve(strict=True)

    if scenario != "incomplete":
        assert workspace._owned_parent is not None
        workspace._owned_parent_identity = preparation._PathIdentity.from_stat(
            workspace._owned_parent.stat()
        )
        identity_source = (
            workspace._owned_directory
            if workspace._owned_directory is not None and workspace._owned_directory.exists()
            else workspace._owned_parent
        )
        workspace._owned_directory_identity = preparation._PathIdentity.from_stat(
            identity_source.stat()
        )

    try:
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match=message,
        ):
            workspace._validated_owned_path()
    finally:
        if cleanup_directory is not None:
            cleanup_directory.rmdir()
        if cleanup_parent is not None:
            cleanup_parent.rmdir()


def test_graceful_exit_cleanup_continues_after_one_workspace_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    original_cleanup = workspace._close_and_remove

    def refuse_cleanup() -> None:
        raise preparation.WorkflowProgressPreparationCleanupRefusedError("injected refusal")

    monkeypatch.setattr(workspace, "_close_and_remove", refuse_cleanup)
    preparation._cleanup_live_workspaces_at_exit()

    assert workspace.path_exists
    assert workspace in preparation._LIVE_WORKSPACES
    monkeypatch.setattr(workspace, "_close_and_remove", original_cleanup)
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_cross_thread_graceful_cleanup_serializes_with_owner_operation(
    tmp_path: Path,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    ready = threading.Event()
    release = threading.Event()
    owner_errors: list[BaseException] = []

    def own_workspace() -> None:
        try:
            workspace.__enter__()
            with workspace._operation_lock:
                ready.set()
                if not release.wait(timeout=10):
                    raise AssertionError("cleanup test did not release the owner thread")
        except BaseException as error:
            owner_errors.append(error)

    owner = threading.Thread(target=own_workspace, name="preparation-owner")
    owner.start()
    assert ready.wait(timeout=10)
    connection = workspace.connection
    assert connection is not None

    preparation._cleanup_live_workspaces_at_exit()

    assert workspace.cleanup_outcome == "busy"
    assert workspace.connection is connection
    assert workspace.path_exists

    release.set()
    owner.join(timeout=10)
    assert not owner.is_alive()
    assert owner_errors == []

    preparation._cleanup_live_workspaces_at_exit()

    assert workspace.connection is None
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists
    assert workspace not in preparation._LIVE_WORKSPACES


def test_workspace_operations_reject_a_non_owner_thread(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    errors: list[BaseException] = []

    with workspace:

        def inspect_pragmas() -> None:
            try:
                workspace.sqlite_pragmas()
            except BaseException as error:
                errors.append(error)

        visitor = threading.Thread(target=inspect_pragmas, name="preparation-visitor")
        visitor.start()
        visitor.join(timeout=10)

        assert not visitor.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert "require the owner thread" in str(errors[0])


def test_connection_close_failure_retains_reference_and_pathless_diagnostic(
    tmp_path: Path,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    assert workspace.connection is not None
    workspace.connection.close()

    class CloseFails:
        def close(self) -> None:
            raise OSError("secret-workspace-name.sqlite3")

    failed_connection = CloseFails()
    workspace.connection = failed_connection  # type: ignore[assignment]

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupOperationalError,
        match="connection close failed",
    ) as captured:
        workspace._close_and_remove()

    assert workspace.cleanup_outcome == "operational_failure"
    assert workspace.connection is failed_connection
    assert "secret-workspace-name" not in str(captured.value)
    assert "secret-workspace-name" not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None

    workspace.connection = None
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_acquisition_filesystem_error_is_pathless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = "customer-secret/owner.lease"
    original_open = Path.open

    def fail_lease(path: Path, *args: Any, **kwargs: Any):
        if path.name == preparation._LEASE_NAME:
            raise OSError(secret_path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_lease)
    with pytest.raises(
        preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
        match="owner lease initialization failed",
    ) as captured:
        preparation.SQLitePreparationWorkspace(parent_directory=tmp_path).__enter__()

    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "failure_type",
    [OSError, RuntimeError, KeyboardInterrupt, GeneratorExit],
)
def test_partial_lease_write_is_removed_for_every_exception_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    secret_path = "customer-secret/partial-owner.lease"
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    def fail_after_partial_write(_value: object, stream: Any, **_kwargs: Any) -> None:
        stream.write("{")
        stream.flush()
        raise failure_type(secret_path)

    monkeypatch.setattr(preparation.json, "dump", fail_after_partial_write)
    with pytest.raises(
        preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
        match="owner lease initialization failed",
    ) as captured:
        workspace.__enter__()

    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists
    assert workspace._lease_identity is None
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "cleanup_failure_type",
    [OSError, RuntimeError, KeyboardInterrupt, GeneratorExit],
)
def test_partial_lease_cleanup_failure_is_bounded_pathless_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure_type: type[BaseException],
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    secret_path = "customer-secret/partial-cleanup"
    original_unlink = Path.unlink

    def fail_after_partial_write(_value: object, stream: Any, **_kwargs: Any) -> None:
        stream.write("{")
        stream.flush()
        raise RuntimeError(secret_path)

    def fail_partial_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name == preparation._LEASE_NAME:
            raise cleanup_failure_type(secret_path)
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(preparation.json, "dump", fail_after_partial_write)
        scoped.setattr(Path, "unlink", fail_partial_unlink)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="partial owner lease removal failed",
        ) as captured:
            workspace.__enter__()

    assert workspace.cleanup_outcome == "operational_failure"
    assert workspace.path_exists
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._remove_partial_owner_lease()
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_partial_lease_cleanup_without_recorded_authority_is_a_noop(tmp_path: Path) -> None:
    workspace, _directory = _unleased_workspace(tmp_path)

    workspace._remove_partial_owner_lease()

    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize("invalid_stage", ["open-handle", "final-path"])
def test_owner_lease_initialization_rejects_nonregular_identity_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_stage: str,
) -> None:
    workspace, directory = _unleased_workspace(tmp_path)
    lease = directory / preparation._LEASE_NAME
    workspace.lease_path = lease
    original_fstat = preparation.os.fstat
    original_stat = preparation.os.stat
    lease_stat_calls = 0

    def as_directory(value: os.stat_result) -> SimpleNamespace:
        return SimpleNamespace(
            st_mode=(preparation.stat.S_IFDIR | preparation.stat.S_IMODE(value.st_mode)),
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_ctime_ns=value.st_ctime_ns,
        )

    def invalid_handle(fd: int) -> os.stat_result | SimpleNamespace:
        value = original_fstat(fd)
        return as_directory(value) if invalid_stage == "open-handle" else value

    def invalid_final_path(path: object, *args: Any, **kwargs: Any):
        nonlocal lease_stat_calls
        value = original_stat(path, *args, **kwargs)
        if invalid_stage == "final-path" and Path(path) == lease:
            lease_stat_calls += 1
            if lease_stat_calls == 1:
                return as_directory(value)
        return value

    with monkeypatch.context() as scoped:
        scoped.setattr(preparation.os, "fstat", invalid_handle)
        scoped.setattr(preparation.os, "stat", invalid_final_path)
        with pytest.raises(
            preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
            match="owner lease initialization failed",
        ):
            workspace._initialize_owner_lease(UUID("00000000-0000-0000-0000-000000000141"))

    assert not lease.exists()
    assert workspace._lease_identity is None
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize(
    ("fault", "error_type", "message"),
    [
        (
            "missing",
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            "partial owner lease is missing or redirected",
        ),
        (
            "inspection",
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            "partial owner lease inspection failed",
        ),
        (
            "identity",
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            "partial owner lease identity changed",
        ),
        (
            "removal",
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            "partial owner lease removal failed",
        ),
        (
            "reappearance",
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            "partial owner lease reappeared",
        ),
    ],
)
def test_partial_lease_cleanup_fails_closed_and_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    error_type: type[Exception],
    message: str,
) -> None:
    workspace, _directory, lease = _partial_lease_workspace(tmp_path)
    original_identity = workspace._lease_identity
    original_stat = preparation.os.stat
    original_unlink = Path.unlink
    secret_path = f"customer-secret/partial-lease-{fault}"

    if fault == "missing":
        lease.unlink()
    elif fault == "identity":
        assert original_identity is not None
        workspace._lease_identity = replace(
            original_identity,
            inode=original_identity.inode + 1,
        )

    def fail_inspection(path: object, *args: Any, **kwargs: Any):
        if fault == "inspection" and Path(path) == lease:
            raise OSError(secret_path)
        return original_stat(path, *args, **kwargs)

    def fail_or_reappear(path: Path, *args: Any, **kwargs: Any) -> None:
        if path != lease:
            original_unlink(path, *args, **kwargs)
            return
        if fault == "removal":
            raise OSError(secret_path)
        original_unlink(path, *args, **kwargs)
        if fault == "reappearance":
            path.write_text("replacement", encoding="utf-8")

    with monkeypatch.context() as scoped:
        if fault == "inspection":
            scoped.setattr(preparation.os, "stat", fail_inspection)
        elif fault in {"removal", "reappearance"}:
            scoped.setattr(Path, "unlink", fail_or_reappear)
        with pytest.raises(error_type, match=message) as captured:
            workspace._remove_partial_owner_lease()

    assert workspace.cleanup_outcome in {"refused", "operational_failure"}
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None

    if lease.exists():
        workspace._lease_identity = preparation._PathIdentity.from_stat(
            os.stat(lease, follow_symlinks=False)
        )
        workspace._remove_partial_owner_lease()
    else:
        workspace._lease_identity = None
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


def test_post_connect_failure_closes_registered_handle_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = preparation.sqlite3.connect
    created: list[Any] = []

    class RowFactoryFails:
        def __init__(self, path: Path) -> None:
            self._connection = original_connect(path)
            self.closed = False

        @property
        def row_factory(self) -> None:
            return None

        @row_factory.setter
        def row_factory(self, _value: object) -> None:
            raise RuntimeError("injected post-connect failure")

        def close(self) -> None:
            self._connection.close()
            self.closed = True

    def connect(path: Path, *_args: Any, **_kwargs: Any) -> RowFactoryFails:
        connection = RowFactoryFails(path)
        created.append(connection)
        return connection

    monkeypatch.setattr(preparation.sqlite3, "connect", connect)
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(RuntimeError, match="injected post-connect failure"):
        workspace.__enter__()

    assert len(created) == 1
    assert created[0].closed
    assert workspace.connection is None
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists
    assert not list(tmp_path.iterdir())


def test_live_workspace_registration_failure_is_inside_acquisition_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    def register_then_fail(value: preparation.SQLitePreparationWorkspace) -> None:
        with preparation._LIVE_WORKSPACES_LOCK:
            preparation._LIVE_WORKSPACES.add(value)
        raise RuntimeError("injected live-workspace registration failure")

    monkeypatch.setattr(preparation, "_register_live_workspace", register_then_fail)
    with pytest.raises(RuntimeError, match="injected live-workspace registration failure"):
        workspace.__enter__()

    assert workspace.connection is None
    assert workspace.cleanup_outcome == "removed"
    assert workspace not in preparation._LIVE_WORKSPACES
    assert not workspace.path_exists
    assert not list(tmp_path.iterdir())


def test_accounting_filesystem_error_is_pathless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    secret_path = "customer-secret/workspace.sqlite3"

    with workspace:
        database_path = workspace.database_path
        assert database_path is not None
        original_stat = preparation.os.stat

        def fail_database_stat(path: object, *args: Any, **kwargs: Any):
            if Path(path) == database_path:
                raise OSError(secret_path)
            return original_stat(path, *args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(preparation.os, "stat", fail_database_stat)
            with pytest.raises(
                preparation.WorkflowProgressPreparationWorkspaceIntegrityError,
                match="workspace accounting failed",
            ) as captured:
                workspace._measure_spill()

        assert secret_path not in str(captured.value)
        assert secret_path not in repr(captured.value)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None


def test_cleanup_filesystem_error_is_pathless_and_operationally_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    database_path = workspace.database_path
    assert database_path is not None
    secret_path = "customer-secret/workspace.sqlite3"
    original_unlink = Path.unlink

    def fail_database_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name == database_path.name:
            raise OSError(secret_path)
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", fail_database_unlink)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="owned-file removal failed",
        ) as captured:
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "operational_failure"
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_cleanup_quarantine_rejects_replacement_after_initial_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    moved = tmp_path / "moved-genuine-workspace"
    original_validate = workspace._validate_owned_directory

    def validate_then_replace(*, acquisition_failure: bool) -> None:
        original_validate(acquisition_failure=acquisition_failure)
        directory.rename(moved)
        directory.mkdir(mode=0o700)

    with monkeypatch.context() as scoped:
        scoped.setattr(workspace, "_validate_owned_directory", validate_then_replace)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match="ownership identity changed",
        ):
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "refused"
    assert workspace._cleanup_quarantine is None
    assert directory.is_dir()
    assert not list(directory.iterdir())
    assert {path.name for path in moved.iterdir()} == {
        preparation._DATABASE_NAME,
        preparation._LEASE_NAME,
    }
    directory.rmdir()
    moved.rename(directory)
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


@pytest.mark.parametrize("replacement", ["source-reappeared", "quarantine-replaced"])
def test_cleanup_quarantine_rejects_post_rename_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    moved = tmp_path / "moved-quarantined-workspace"
    original_quarantine = workspace._quarantine_owned_directory

    def quarantine_then_replace() -> Path:
        quarantine = original_quarantine()
        if replacement == "source-reappeared":
            directory.mkdir(mode=0o700)
        else:
            quarantine.rename(moved)
            quarantine.mkdir(mode=0o700)
        return quarantine

    message = (
        "source reappeared" if replacement == "source-reappeared" else "quarantine identity changed"
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(workspace, "_quarantine_owned_directory", quarantine_then_replace)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match=message,
        ):
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "refused"
    quarantine = workspace._cleanup_quarantine
    assert quarantine is not None
    assert workspace.path_exists
    if replacement == "source-reappeared":
        directory.rmdir()
    else:
        quarantine.rmdir()
        moved.rename(quarantine)
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_cleanup_quarantine_rename_error_is_pathless_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    secret_path = "customer-secret/quarantine-target"
    original_rename = Path.rename

    def fail_quarantine_rename(path: Path, target: Path) -> Path:
        if path == directory:
            raise OSError(secret_path)
        return original_rename(path, target)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "rename", fail_quarantine_rename)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="quarantine rename failed",
        ) as captured:
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "operational_failure"
    assert workspace.path_exists
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


@pytest.mark.parametrize("fault", ["inspection", "unexpected-entry"])
def test_unleased_cleanup_revalidates_after_quarantine_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    workspace, _directory = _unleased_workspace(tmp_path)
    original_iterdir = Path.iterdir
    original_quarantine = workspace._quarantine_owned_directory
    secret_path = f"customer-secret/unleased-quarantine-{fault}"

    def fail_quarantine_inspection(path: Path):
        if fault == "inspection" and path == workspace._cleanup_quarantine:
            raise OSError(secret_path)
        return original_iterdir(path)

    def quarantine_then_add_entry() -> Path:
        quarantine = original_quarantine()
        if fault == "unexpected-entry":
            (quarantine / "replacement.txt").write_text("unsafe", encoding="utf-8")
        return quarantine

    with monkeypatch.context() as scoped:
        if fault == "inspection":
            scoped.setattr(Path, "iterdir", fail_quarantine_inspection)
        else:
            scoped.setattr(workspace, "_quarantine_owned_directory", quarantine_then_add_entry)
        error_type = (
            preparation.WorkflowProgressPreparationCleanupOperationalError
            if fault == "inspection"
            else preparation.WorkflowProgressPreparationCleanupRefusedError
        )
        message = (
            "unleased-directory inspection failed"
            if fault == "inspection"
            else "quarantined unleased directory is not empty"
        )
        with pytest.raises(error_type, match=message) as captured:
            workspace._remove_empty_acquisition_directory()

    assert secret_path not in str(captured.value)
    assert workspace.path_exists
    quarantine = workspace._cleanup_quarantine
    assert quarantine is not None
    replacement = quarantine / "replacement.txt"
    if replacement.exists():
        replacement.unlink()
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_quarantine_collision_refuses_without_renaming_owned_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    collision_id = "00000000-0000-0000-0000-000000000141"
    collision = tmp_path / f"{preparation._QUARANTINE_PREFIX}{collision_id}"
    collision.mkdir(mode=0o700)
    monkeypatch.setattr(preparation, "uuid4", lambda: collision_id)

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="quarantine already exists",
    ):
        workspace._close_and_remove()

    assert workspace.cleanup_outcome == "refused"
    assert directory.is_dir()
    assert workspace._cleanup_quarantine is None
    collision.rmdir()
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("missing", "quarantine is missing"),
        ("inspection", "quarantine inspection failed"),
        ("wrong-type", "quarantine was replaced or redirected"),
        ("invalid-name", "quarantine has no canonical UUID"),
        ("unsafe-parent", "cleanup parent became unsafe"),
        ("wrong-path", "quarantine path changed"),
    ],
)
def test_cleanup_quarantine_validation_faults_are_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    message: str,
) -> None:
    workspace, _directory, quarantine = _quarantined_workspace(tmp_path)
    assert workspace._quarantine_owned_directory() == quarantine
    moved = tmp_path / "moved-valid-quarantine"
    invalid = tmp_path / f"{preparation._QUARANTINE_PREFIX}not-a-uuid"
    original_stat = preparation.os.stat

    if fault == "missing":
        quarantine.rename(moved)
    elif fault == "wrong-type":
        quarantine.rename(moved)
        quarantine.write_text("replacement", encoding="utf-8")
    elif fault == "invalid-name":
        quarantine.rename(invalid)
        workspace._cleanup_quarantine = invalid

    def fail_inspection(path: object, *args: Any, **kwargs: Any):
        if fault == "inspection" and Path(path) == quarantine:
            raise OSError("customer-secret/quarantine-inspection")
        return original_stat(path, *args, **kwargs)

    def reject_parent(_parent_stat: os.stat_result) -> None:
        raise preparation.WorkflowProgressPreparationWorkspaceAcquisitionError(
            "injected unsafe cleanup parent"
        )

    with monkeypatch.context() as scoped:
        if fault == "inspection":
            scoped.setattr(preparation.os, "stat", fail_inspection)
        elif fault == "unsafe-parent":
            scoped.setattr(workspace, "_validate_acquisition_parent_stat", reject_parent)
        with pytest.raises(
            (
                preparation.WorkflowProgressPreparationCleanupOperationalError
                if fault == "inspection"
                else preparation.WorkflowProgressPreparationCleanupRefusedError
            ),
            match=message,
        ):
            if fault == "wrong-path":
                workspace._validate_quarantined_directory(
                    tmp_path / "different-quarantine",
                    acquisition_failure=True,
                    require_lease=False,
                )
            else:
                workspace._validated_cleanup_quarantine()

    if fault == "missing":
        moved.rename(quarantine)
    elif fault == "wrong-type":
        quarantine.unlink()
        moved.rename(quarantine)
    elif fault == "invalid-name":
        invalid.rename(quarantine)
        workspace._cleanup_quarantine = quarantine
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_retry_accepts_validated_quarantine_after_file_removal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    original_rmdir = Path.rmdir
    failed_once = False

    def fail_quarantine_rmdir_once(path: Path) -> None:
        nonlocal failed_once
        if path == workspace._cleanup_quarantine and not failed_once:
            failed_once = True
            raise OSError("customer-secret/quarantine-rmdir")
        original_rmdir(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "rmdir", fail_quarantine_rmdir_once)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="owned-file removal failed",
        ):
            workspace._close_and_remove()

    quarantine = workspace._cleanup_quarantine
    assert quarantine is not None
    assert not list(quarantine.iterdir())
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_retry_refuses_entry_created_after_owned_files_are_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    original_unlink = Path.unlink
    replacement: Path | None = None

    def add_entry_after_lease_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal replacement
        original_unlink(path, *args, **kwargs)
        if path.name == preparation._LEASE_NAME:
            replacement = path.parent / "replacement.txt"
            replacement.write_text("unsafe", encoding="utf-8")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", add_entry_after_lease_unlink)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match="quarantine contains an unowned entry",
        ):
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "refused"
    assert replacement is not None
    replacement.unlink()
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_quarantine_operational_validation_error_is_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _directory, quarantine = _quarantined_workspace(tmp_path)
    original_validate = workspace._validated_cleanup_quarantine
    calls = 0

    def fail_during_removal() -> Path:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise preparation.WorkflowProgressPreparationCleanupOperationalError(
                "injected quarantine validation failure"
            )
        return original_validate()

    with monkeypatch.context() as scoped:
        scoped.setattr(workspace, "_validated_cleanup_quarantine", fail_during_removal)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="injected quarantine validation failure",
        ):
            workspace._remove_quarantined_directory(quarantine, leased=True)

    assert workspace.cleanup_outcome == "operational_failure"
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize("replacement", ["database-type", "lease-identity"])
def test_cleanup_revalidates_each_entry_immediately_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    workspace, _directory, quarantine = _quarantined_workspace(tmp_path)
    original_validate = workspace._validated_cleanup_quarantine
    moved = tmp_path / f"original-{replacement}"
    calls = 0

    def replace_before_entry_unlink() -> Path:
        nonlocal calls
        calls += 1
        if replacement == "database-type" and calls == 4:
            database = quarantine / preparation._DATABASE_NAME
            database.rename(moved)
            database.mkdir()
        elif replacement == "lease-identity" and calls == 5:
            lease = quarantine / preparation._LEASE_NAME
            lease.rename(moved)
            lease.write_bytes(moved.read_bytes())
        return original_validate()

    message = (
        "cleanup entry identity changed"
        if replacement == "database-type"
        else "owner lease identity changed"
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(
            workspace,
            "_validated_cleanup_quarantine",
            replace_before_entry_unlink,
        )
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match=message,
        ):
            workspace._remove_quarantined_directory(quarantine, leased=True)

    assert workspace.cleanup_outcome == "refused"
    replaced_path = quarantine / (
        preparation._DATABASE_NAME if replacement == "database-type" else preparation._LEASE_NAME
    )
    if replacement == "database-type":
        replaced_path.rmdir()
    else:
        replaced_path.unlink()
    moved.rename(replaced_path)
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_refuses_replaced_owner_lease_identity_after_rename(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    lease = workspace.lease_path
    assert lease is not None
    original_lease = tmp_path / "original-owner-lease"
    lease.rename(original_lease)
    lease.write_bytes(original_lease.read_bytes())
    if os.name == "posix":
        lease.chmod(0o600)

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="owner lease identity changed",
    ):
        workspace._close_and_remove()

    assert workspace.cleanup_outcome == "refused"
    lease.unlink()
    original_lease.rename(lease)
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_quarantine_rechecks_source_after_identity_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, directory, _quarantine = _quarantined_workspace(tmp_path)
    original_lexists = preparation.os.path.lexists
    source_checks = 0

    def reappear_on_second_check(path: object) -> bool:
        nonlocal source_checks
        if Path(path) == directory:
            source_checks += 1
            return source_checks == 2
        return original_lexists(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(preparation.os.path, "lexists", reappear_on_second_check)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match="source reappeared",
        ):
            workspace._validated_cleanup_quarantine()

    assert source_checks == 2
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_quarantine_internal_authority_invariants_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unleased_parent = tmp_path / "unleased"
    unleased_parent.mkdir()
    unleased, directory = _unleased_workspace(unleased_parent)
    parent = unleased._owned_parent
    assert parent is not None
    unleased._owned_parent = None
    with monkeypatch.context() as scoped:
        scoped.setattr(unleased, "_validated_owned_path", lambda: directory)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match="cleanup parent is unavailable",
        ):
            unleased._quarantine_owned_directory()
    unleased._owned_parent = parent
    unleased._remove_empty_acquisition_directory()

    incomplete = preparation.SQLitePreparationWorkspace()
    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="quarantine identity is incomplete",
    ):
        incomplete._validated_cleanup_quarantine()

    leased_parent = tmp_path / "leased"
    leased_parent.mkdir()
    workspace, directory, quarantine = _quarantined_workspace(leased_parent)
    workspace._owned_directory = None
    with monkeypatch.context() as scoped:
        scoped.setattr(workspace, "_validated_cleanup_quarantine", lambda: quarantine)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match="workspace identity is unavailable",
        ):
            workspace._validate_quarantined_directory(
                quarantine,
                acquisition_failure=True,
                require_lease=False,
            )
    workspace._owned_directory = directory
    workspace._close_and_remove()


def test_cleanup_quarantine_requires_lease_before_initial_removal(tmp_path: Path) -> None:
    workspace, _directory, quarantine = _quarantined_workspace(tmp_path)
    lease = quarantine / preparation._LEASE_NAME
    moved = tmp_path / "temporarily-missing-owner-lease"
    lease.rename(moved)

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupOperationalError,
        match="owner lease read failed",
    ):
        workspace._validate_quarantined_directory(
            quarantine,
            acquisition_failure=True,
            require_lease=True,
        )

    moved.rename(lease)
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize(
    ("phase", "message"),
    [
        ("inventory", "cleanup inventory failed"),
        ("entry", "cleanup entry inspection failed"),
        ("lease-stat", "owner lease read failed"),
        ("lease-read", "owner lease read failed"),
    ],
)
def test_cleanup_inspection_errors_are_pathless_and_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    message: str,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    database_path = workspace.database_path
    lease_path = workspace.lease_path
    assert directory is not None
    assert database_path is not None
    assert lease_path is not None
    secret_path = f"customer-secret/cleanup-{phase}"
    original_iterdir = Path.iterdir
    original_read_text = Path.read_text
    original_stat = preparation.os.stat
    lease_stat_calls = 0

    def fail_inventory(path: Path):
        if path == directory:
            raise OSError(secret_path)
        return original_iterdir(path)

    def fail_entry_or_lease_stat(path: object, *args: Any, **kwargs: Any):
        nonlocal lease_stat_calls
        candidate = Path(path)
        if phase == "entry" and candidate == database_path:
            raise OSError(secret_path)
        if phase == "lease-stat" and candidate == lease_path:
            lease_stat_calls += 1
            if lease_stat_calls == 2:
                raise OSError(secret_path)
        return original_stat(path, *args, **kwargs)

    def fail_lease_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == lease_path:
            raise OSError(secret_path)
        return original_read_text(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        if phase == "inventory":
            scoped.setattr(Path, "iterdir", fail_inventory)
        elif phase in {"entry", "lease-stat"}:
            scoped.setattr(preparation.os, "stat", fail_entry_or_lease_stat)
        else:
            scoped.setattr(Path, "read_text", fail_lease_read)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match=message,
        ) as captured:
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "operational_failure"
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists
