"""Closed, setup-only Linux child for the isolated startup experiment."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

ROOTS = (Path("/app/src"), Path("/app/testproject"))
MAX_FILES = 4096
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_REPORT = 8192
SETUP_SECONDS = 19
GUARDED_MODULES = frozenset({"django.db.backends.base.base", "psycopg", "psycopg2"})


def source_manifest(roots=ROOTS):
    """Hash regular source files and modes, without importing measured modules."""
    digest = hashlib.sha256()
    files = []
    total = 0
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("source_symlink")
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_dir():
                continue
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("source_type")
            total += info.st_size
            if total > MAX_SOURCE_BYTES or len(files) >= MAX_FILES:
                raise ValueError("source_limit")
            raw = path.read_bytes()
            relative = root.name + "/" + path.relative_to(root).as_posix()
            digest.update(json.dumps([relative, stat.S_IMODE(info.st_mode)]).encode() + b"\0")
            digest.update(hashlib.sha256(raw).digest())
            files.append(path)
    if not files:
        raise ValueError("source_empty")
    return digest.hexdigest(), files


def cache_valid(path):
    """Accept only this interpreter's checked-hash cache of these source bytes."""
    cached = Path(importlib.util.cache_from_source(str(path)))
    if not cached.is_file() or cached.is_symlink():
        return False
    with cached.open("rb") as stream:
        header = stream.read(16)
    return (
        len(header) == 16
        and header[:4] == importlib.util.MAGIC_NUMBER
        and int.from_bytes(header[4:8], "little") == 3
        and header[8:] == importlib.util.source_hash(path.read_bytes())
    )


def census(roots=ROOTS):
    source_hash, files = source_manifest(roots)
    python = [path for path in files if path.suffix == ".py"]
    return {
        "schema_version": 1,
        "kind": "census",
        "source_sha256": source_hash,
        "source_files": len(files),
        "python_files": len(python),
        "checked_hash_caches": sum(cache_valid(path) for path in python),
        "cache_files": sum(1 for root in roots for path in root.rglob("*.pyc") if path.is_file()),
        "python_version": list(sys.version_info[:3]),
        "cache_tag": sys.implementation.cache_tag,
        "optimize": sys.flags.optimize,
    }


class RefusedError(RuntimeError):
    """A fixed guard refused an operation, even if application code catches it."""


class Guards:
    def __init__(self):
        self.hit = False

    def refuse(self, *args, **kwargs):
        self.hit = True
        raise RefusedError("forbidden_operation")

    def audit(self, event, args):
        if event in {
            "socket.connect",
            "socket.connect_ex",
            "socket.getaddrinfo",
            "socket.sendto",
            "subprocess.Popen",
            "os.system",
            "os.posix_spawn",
            "os.exec",
            "os.fork",
        }:
            self.refuse()

    def patch(self, name, module):
        if name == "django.db.backends.base.base":
            module.BaseDatabaseWrapper.connect = self.refuse
        else:
            module.connect = self.refuse
            if name == "psycopg":
                module.Connection.connect = self.refuse
                module.AsyncConnection.connect = self.refuse

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ray" or fullname.startswith("ray."):
            self.refuse()
        if fullname not in GUARDED_MODULES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.loader is None:
            self.refuse()
        original = spec.loader
        guard = self

        class Loader:
            def create_module(self, spec):
                return original.create_module(spec)

            def exec_module(self, module):
                original.exec_module(module)
                guard.patch(fullname, module)

        spec.loader = Loader()
        return spec

    def install(self):
        if any(name == "ray" or name.startswith("ray.") for name in sys.modules):
            self.refuse()
        if any(name in sys.modules for name in GUARDED_MODULES):
            self.refuse()
        sys.meta_path.insert(0, self)
        sys.addaudithook(self.audit)


def imported_source_caches():
    """Fixed-root provenance after timing; never emit imported module names."""
    count = valid = 0
    for module in tuple(sys.modules.values()):
        name = getattr(module, "__file__", None)
        if not isinstance(name, str):
            continue
        path = Path(name)
        if any(path.is_relative_to(root) for root in ROOTS):
            if path.suffix != ".py" or path.is_symlink():
                raise ValueError("module_origin")
            expected = importlib.util.cache_from_source(name)
            if getattr(module, "__cached__", None) != expected:
                raise ValueError("module_cache_origin")
            count += 1
            valid += cache_valid(path)
    for name, expected in {
        "django_ray": "/app/src/django_ray/__init__.py",
        "testproject.settings_qualification": "/app/testproject/settings_qualification.py",
    }.items():
        if getattr(sys.modules.get(name), "__file__", None) != expected:
            raise ValueError("module_origin")
    return count, valid


def setup_child(fd):
    # Guard installation must not import Django, settings, database or Ray modules.
    guard = Guards()
    outcome = "setup_error"
    wall = cpu = None
    imported = cached = 0
    try:
        sys.path[:0] = ["/app/src", "/app"]
        guard.install()
        started, started_cpu = time.monotonic(), time.process_time()
        import django

        django.setup()
        wall = round((time.monotonic() - started) * 1000)
        cpu = round((time.process_time() - started_cpu) * 1000)
        imported, cached = imported_source_caches()
        outcome = "complete"
    except BaseException:
        pass
    if guard.hit:
        outcome = "guard_refused"
    record = {
        "outcome": outcome,
        "import_setup_wall_ms": wall,
        "import_setup_cpu_ms": cpu,
        "imported_source_modules": imported,
        "imported_checked_caches": cached,
    }
    os.write(fd, json.dumps(record, separators=(",", ":")).encode())


def measure():
    """The owning supervisor collects CPU even when its child reaches 19 seconds."""
    read_fd, write_fd = os.pipe()
    started = time.monotonic()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        try:
            setup_child(write_fd)
        finally:
            os._exit(0)
    os.close(write_fd)
    timed_out = False
    while True:
        waited, status, usage = os.wait4(child, os.WNOHANG)
        if waited:
            break
        if time.monotonic() - started >= SETUP_SECONDS:
            timed_out = True
            os.kill(child, signal.SIGKILL)
            _, status, usage = os.wait4(child, 0)
            break
        time.sleep(0.01)
    raw = os.read(read_fd, MAX_REPORT + 1)
    os.close(read_fd)
    record = {
        "outcome": "timeout" if timed_out else "child_failed",
        "import_setup_wall_ms": None,
        "import_setup_cpu_ms": None,
        "imported_source_modules": 0,
        "imported_checked_caches": 0,
    }
    if not timed_out and os.waitstatus_to_exitcode(status) == 0 and 0 < len(raw) <= MAX_REPORT:
        record = json.loads(raw)
    return {
        "schema_version": 1,
        "kind": "sample",
        **record,
        "child_wall_ms": round((time.monotonic() - started) * 1000),
        "child_cpu_ms": round((usage.ru_utime + usage.ru_stime) * 1000),
    }


def main():
    if sys.platform != "linux" or len(sys.argv) != 2 or sys.argv[1] not in {"census", "sample"}:
        return 2
    # Preserve one protocol FD; all ambient output, including census errors, is discarded.
    protocol = os.dup(1)
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    try:
        value = census() if sys.argv[1] == "census" else measure()
        raw = json.dumps(value, separators=(",", ":")).encode() + b"\n"
        if len(raw) > MAX_REPORT:
            return 2
        os.write(protocol, raw)
        return 0
    except BaseException:
        return 2
    finally:
        os.close(protocol)


if __name__ == "__main__":
    raise SystemExit(main())
