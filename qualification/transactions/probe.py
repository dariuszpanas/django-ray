"""Run the receipt suite against one private, socket-only PostgreSQL child."""

from __future__ import annotations

import importlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from qualification.docker import scenario as wheel
from qualification.transactions.contract import MAX_PROBE_BYTES, case_nodeids

POSTGRES_BIN = Path("/usr/lib/postgresql/17/bin")


def stop_server(server):
    """Require a clean fast shutdown; forced termination cannot be passing proof."""
    if server.poll() is not None:
        raise wheel.QualificationError("postgres-exited-before-cleanup")
    server.send_signal(signal.SIGINT)
    try:
        code = server.wait(timeout=15)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)
        raise wheel.QualificationError("postgres-shutdown-timeout") from None
    if code != 0:
        raise wheel.QualificationError("postgres-shutdown-failed")


def execute(root, expected_module, receipt_path):
    if not __debug__:
        raise wheel.QualificationError("transaction qualification requires assertions enabled")
    wheel._require_non_root()
    wheel._require_linux_process_groups()
    psycopg = importlib.import_module("psycopg")

    if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
        raise wheel.QualificationError("expected-empty-transaction-fixture")
    data, socket = root / "data", root / "socket"
    socket.mkdir(mode=0o700)
    environment = wheel._subprocess_environment()
    init = wheel._run_bounded_command(
        (
            str(POSTGRES_BIN / "initdb"),
            "-D",
            str(data),
            "-U",
            "qualification",
            "--auth-local=trust",
            "--auth-host=reject",
            "--no-sync",
            "--no-locale",
            "-E",
            "UTF8",
        ),
        cwd=root,
        env=environment,
        timeout=30,
    )
    if init.returncode != 0:
        wheel._emit_failure_output("postgres-initdb-stdout", init.stdout)
        wheel._emit_failure_output("postgres-initdb-stderr", init.stderr)
        raise wheel.QualificationError("postgres-initialization-failed")
    # Keep the server in this probe's process group so the outer timeout can
    # stop every descendant. No pg_ctl daemon, TCP listener or shared database.
    server = subprocess.Popen(
        (
            str(POSTGRES_BIN / "postgres"),
            "-D",
            str(data),
            "-k",
            str(socket),
            "-h",
            "",
            "-c",
            "max_connections=10",
            "-c",
            "shared_buffers=16MB",
            "-c",
            "work_mem=1MB",
            "-c",
            "temp_file_limit=32768",
            "-c",
            "max_wal_size=64MB",
            "-c",
            "min_wal_size=32MB",
        ),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            if server.poll() is not None:
                raise wheel.QualificationError("postgres-startup-failed")
            try:
                with psycopg.connect(
                    host=str(socket), user="qualification", dbname="postgres", connect_timeout=1
                ):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise wheel.QualificationError("postgres-readiness-timeout") from None
                time.sleep(0.1)
        os.environ.update(
            DJANGO_SETTINGS_MODULE="qualification.transactions.settings",
            DJANGO_RAY_TRANSACTION_SOCKET=str(socket),
            DJANGO_RAY_TRANSACTION_MODULE=expected_module,
            DJANGO_RAY_TRANSACTION_RECEIPT=str(receipt_path),
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        )
        import pytest

        from django_ray.execution_protocol import EXECUTION_PROTOCOL_VERSION

        code = pytest.main(
            [
                "-p",
                "django",
                "-p",
                "qualification.transactions.plugin",
                *case_nodeids(EXECUTION_PROTOCOL_VERSION),
                "-m",
                "postgresql",
                "-q",
                "-o",
                "addopts=",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(root / "pytest"),
            ]
        )
    finally:
        stop_server(server)
    receipt = json.loads(wheel._bounded_regular_bytes(receipt_path, maximum=MAX_PROBE_BYTES))
    receipt["server_stopped"] = True
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    return code


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("expected an owned fixture, installed module and receipt path")
    raise SystemExit(execute(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])))
