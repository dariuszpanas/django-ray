"""Focused filesystem cleanup integrity and preservation contracts."""

import builtins
import os
import stat
import subprocess
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import django_ray.result_storage as result_storage
from django_ray.result_storage import (
    FilesystemResultStorage,
    ResultStorageError,
    ResultStorageIntegrityError,
)


@pytest.mark.parametrize("difference", [-1, 1])
def test_delete_wrong_declared_size_preserves_shared_digest_object(tmp_path, difference):

    storage = FilesystemResultStorage(tmp_path)

    payload = "shared input or request bytes"

    reference = storage.store_payload(serialized_payload=payload)

    historical = reference.rsplit("=", 1)[0] + f"={len(payload.encode()) + difference}"

    with pytest.raises(ResultStorageIntegrityError, match="integrity verification"):
        storage.delete(reference=historical)

    assert storage.load(reference=reference) == payload


def test_delete_wrong_digest_preserves_existing_file(tmp_path):

    storage = FilesystemResultStorage(tmp_path)

    reference = storage.store_payload(serialized_payload="original")

    path = next(tmp_path.rglob("*.json"))

    path.write_bytes(b"replaced")

    with pytest.raises(ResultStorageIntegrityError, match="integrity verification"):
        storage.delete(reference=reference)

    assert path.read_bytes() == b"replaced"


def test_delete_missing_file_never_unlinks_a_later_object(tmp_path, monkeypatch):

    storage = FilesystemResultStorage(tmp_path)

    reference = storage.store_payload(serialized_payload="original")

    path = next(tmp_path.rglob("*.json"))

    path.unlink()

    unlink = Mock(side_effect=AssertionError("absence cannot authorize a later deletion"))

    monkeypatch.setattr(Path, "unlink", unlink)

    assert storage.delete(reference=reference) is None

    unlink.assert_not_called()


class _Reader:
    def __init__(self, handle, read):

        self.handle = handle

        self.read = read

    def __enter__(self):

        self.handle.__enter__()

        return self

    def __exit__(self, *args):

        return self.handle.__exit__(*args)

    def fileno(self):

        return self.handle.fileno()


def _observe_reads(monkeypatch, *, failure=None):

    sizes = []

    handles = []

    def open_file(*args, **kwargs):

        handle = builtins.open(*args, **kwargs)

        handles.append(handle)

        def read(size):

            sizes.append(size)

            if failure:
                raise OSError("private-provider-detail")

            return handle.read(size)

        return _Reader(handle, read)

    monkeypatch.setattr(result_storage, "open", open_file, raising=False)

    return sizes, handles


@pytest.mark.parametrize("payload", ["", "é" * 100_000], ids=["empty", "multichunk"])
def test_delete_verifies_bounded_chunks_without_materializing_payload(
    tmp_path, monkeypatch, payload
):

    storage = FilesystemResultStorage(tmp_path)

    reference = storage.store_payload(serialized_payload=payload)

    sizes, handles = _observe_reads(monkeypatch)

    storage.delete(reference=reference)

    assert sizes and all(0 < size <= 64 * 1024 for size in sizes)

    assert sum(sizes) == len(payload.encode()) + 1

    assert all(handle.closed for handle in handles)

    assert list(tmp_path.rglob("*.json")) == []


def test_delete_rejects_size_before_reading_payload(tmp_path, monkeypatch):

    storage = FilesystemResultStorage(tmp_path)

    reference = storage.store_payload(serialized_payload="preserved")

    sizes, handles = _observe_reads(monkeypatch)

    with pytest.raises(ResultStorageIntegrityError):
        storage.delete(reference=reference.rsplit("=", 1)[0] + "=1")

    assert sizes == []

    assert all(handle.closed for handle in handles)

    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_delete_read_failure_is_sanitized_and_preserves_file(tmp_path, monkeypatch):

    storage = FilesystemResultStorage(tmp_path)

    reference = storage.store_payload(serialized_payload="preserved")

    _sizes, handles = _observe_reads(monkeypatch, failure=True)

    with pytest.raises(ResultStorageError, match="verify filesystem payload") as caught:
        storage.delete(reference=reference)

    assert "private-provider-detail" not in "".join(traceback.format_exception(caught.value))

    assert caught.value.__context__ is None

    assert all(handle.closed for handle in handles)

    assert len(list(tmp_path.rglob("*.json"))) == 1


def _changed_stat(value, field):

    fields = {
        name: getattr(value, name)
        for name in ("st_dev", "st_ino", "st_size", "st_mode", "st_mtime_ns", "st_ctime_ns")
    }

    fields[field] += 1

    return SimpleNamespace(**fields)


@pytest.mark.parametrize("field", ["st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"])
def test_delete_refuses_opened_file_mutation_during_verification(tmp_path, monkeypatch, field):

    storage = FilesystemResultStorage(tmp_path)

    reference = storage.store_payload(serialized_payload="preserved")

    original = result_storage.os.fstat

    calls = 0

    def fstat(descriptor):

        nonlocal calls

        calls += 1

        value = original(descriptor)

        return value if calls == 1 else _changed_stat(value, field)

    monkeypatch.setattr(result_storage.os, "fstat", fstat)

    with pytest.raises(ResultStorageIntegrityError):
        storage.delete(reference=reference)

    assert calls == 2

    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_delete_refuses_observed_path_replacement_after_verification(tmp_path, monkeypatch):

    storage = FilesystemResultStorage(tmp_path)

    reference = storage.store_payload(serialized_payload="preserved")

    original = Path.stat

    def stat_path(path, **kwargs):

        value = original(path, **kwargs)

        return _changed_stat(value, "st_ino") if kwargs.get("follow_symlinks") is False else value

    monkeypatch.setattr(Path, "stat", stat_path)

    with pytest.raises(ResultStorageIntegrityError):
        storage.delete(reference=reference)

    assert len(list(tmp_path.rglob("*.json"))) == 1


@pytest.mark.parametrize("change", ["truncate", "grow"])
def test_delete_refuses_length_change_while_reading(tmp_path, monkeypatch, change):
    storage = FilesystemResultStorage(tmp_path)
    reference = storage.store_payload(serialized_payload="preserved")
    path = next(tmp_path.rglob("*.json"))
    changed = b"short" if change == "truncate" else b"preserved-extra"

    def open_file(*args, **kwargs):
        handle = builtins.open(*args, **kwargs)
        first = True

        def read(size):
            nonlocal first
            if first:
                first = False
                path.write_bytes(changed)
            return handle.read(size)

        return _Reader(handle, read)

    monkeypatch.setattr(result_storage, "open", open_file, raising=False)
    with pytest.raises(ResultStorageIntegrityError):
        storage.delete(reference=reference)

    assert path.read_bytes() == changed


@pytest.mark.parametrize("failure", ["open", "descriptor", "path"])
def test_delete_observation_errors_preserve_object_without_private_context(
    tmp_path, monkeypatch, failure
):
    storage = FilesystemResultStorage(tmp_path)
    reference = storage.store_payload(serialized_payload="preserved")

    def unavailable(*args, **kwargs):
        raise OSError("private-provider-detail")

    if failure == "open":
        monkeypatch.setattr(result_storage, "open", unavailable, raising=False)
    elif failure == "descriptor":
        monkeypatch.setattr(result_storage.os, "fstat", unavailable)
    else:
        original = Path.stat

        def stat_path(path, **kwargs):
            if kwargs.get("follow_symlinks") is False:
                raise FileNotFoundError("private-provider-detail")
            return original(path, **kwargs)

        monkeypatch.setattr(Path, "stat", stat_path)
    with pytest.raises(ResultStorageError, match="verify filesystem payload") as caught:
        storage.delete(reference=reference)

    assert caught.value.__context__ is None
    assert "private-provider-detail" not in "".join(traceback.format_exception(caught.value))
    monkeypatch.undo()
    assert storage.load(reference=reference) == "preserved"


def test_delete_refuses_nonregular_descriptor_before_any_read(tmp_path, monkeypatch):
    storage = FilesystemResultStorage(tmp_path)
    reference = storage.store_payload(serialized_payload="preserved")
    sizes, handles = _observe_reads(monkeypatch)
    original = result_storage.os.fstat

    def fstat(descriptor):
        value = _changed_stat(original(descriptor), "st_mode")
        value.st_mode = stat.S_IFIFO
        return value

    monkeypatch.setattr(result_storage.os, "fstat", fstat)
    with pytest.raises(ResultStorageIntegrityError):
        storage.delete(reference=reference)

    assert sizes == []
    assert all(handle.closed for handle in handles)
    assert len(list(tmp_path.rglob("*.json"))) == 1


@pytest.mark.skipif(sys.platform != "linux", reason="Linux filesystem admission contract")
def test_delete_real_fifo_without_writer_refuses_within_subprocess_deadline(tmp_path):
    storage = FilesystemResultStorage(tmp_path)
    reference = storage.store_payload(serialized_payload="preserved")
    path = next(tmp_path.rglob("*.json"))
    path.unlink()
    os.mkfifo(path)

    # Without O_NONBLOCK, opening a FIFO with no writer hangs before fstat can
    # reject it. Keep the regression itself bounded and leave the object intact.
    code = """
import sys
from django_ray.result_storage import FilesystemResultStorage, ResultStorageIntegrityError
try:
    FilesystemResultStorage(sys.argv[1]).delete(reference=sys.argv[2])
except ResultStorageIntegrityError:
    print("nonregular-refused")
else:
    raise SystemExit("nonregular object was accepted")
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), reference],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "nonregular-refused"
    assert stat.S_ISFIFO(path.stat(follow_symlinks=False).st_mode)
