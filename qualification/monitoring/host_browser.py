"""Own the loopback forward and limited browser; never signal a reaped PID."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import time

from qualification.monitoring.run import CONTEXT, command


def stop_forward(process) -> None:
    """Hold the leader unreaped through the final group signal."""
    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    time.sleep(0.2)
    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def observe(*, namespace: str, image: str, credentials: dict[str, str], deadline: float) -> dict:
    if deadline - time.monotonic() < 150:
        raise TimeoutError("Insufficient remaining observation budget for browser")
    name = namespace + "-browser"
    if command(["docker", "ps", "-aq", "--filter", "name=^" + name + "$"], seconds=5).strip():
        raise ValueError("Owned browser container name is occupied")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 30030))
    with tempfile.TemporaryFile() as output:
        forward = subprocess.Popen(
            [
                "kubectl",
                "--context",
                CONTEXT,
                "--request-timeout=20s",
                "-n",
                namespace,
                "port-forward",
                "--address=127.0.0.1",
                "service/grafana-svc",
                "30030:3000",
            ],
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
        try:
            ready = time.monotonic() + 10
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", 30030), timeout=1):
                        break
                except OSError:
                    if time.monotonic() >= ready:
                        raise TimeoutError("Owned loopback forward is unavailable") from None
                    time.sleep(0.1)
            args = [
                "docker",
                "run",
                "--name",
                name,
                "--label",
                "qualification.django-ray/namespace=" + namespace,
                "--network=host",
                "--cpus=0.25",
                "--memory=512m",
                "--memory-swap=512m",
                "--pids-limit=256",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--tmpfs",
                "/tmp:rw,nosuid,size=96m",
                "--shm-size=128m",
                "--env",
                "GF_SECURITY_ADMIN_USER",
                "--env",
                "GF_SECURITY_ADMIN_PASSWORD",
                "--entrypoint",
                "/opt/qualification-browser/bin/python",
                image,
                "-m",
                "qualification.monitoring.browser",
            ]
            receipt = json.loads(command(args, seconds=95, env={**os.environ, **credentials}))
            details = json.loads(command(["docker", "inspect", name], seconds=5))[0]
            limits = details["HostConfig"]
            if (
                (limits["NanoCpus"], limits["Memory"], limits["MemorySwap"], limits["PidsLimit"])
                != (250000000, 512 * 1024**2, 512 * 1024**2, 256)
                or details["State"].get("OOMKilled")
                or details["State"]["ExitCode"] != 0
                or receipt.get("status") != "passed"
            ):
                raise ValueError("Owned browser did not meet its capped contract")
            receipt.update(
                cpu_limit_millicores=250, memory_limit_mib=512, pids_limit=256, oom_killed=False
            )
        finally:
            try:
                # The name was proven absent immediately before our only create.
                # A disposable VM and the exact namespace label bound ownership.
                ids = command(
                    [
                        "docker",
                        "ps",
                        "-aq",
                        "--filter",
                        "name=^" + name + "$",
                        "--filter",
                        "label=qualification.django-ray/namespace=" + namespace,
                    ],
                    seconds=5,
                ).strip()
                if ids:
                    command(["docker", "rm", "-f", name], seconds=10)
                if command(
                    ["docker", "ps", "-aq", "--filter", "name=^" + name + "$"], seconds=5
                ).strip():
                    raise ValueError("Owned browser cleanup was not confirmed")
            finally:
                stop_forward(forward)
    if time.monotonic() >= deadline:
        raise TimeoutError("Browser observation and cleanup exceeded the shared deadline")
    receipt["cleanup_confirmed"] = True
    return receipt
