"""Run one time-bounded, loopback-only sample service port forward."""

from __future__ import annotations

import argparse
import re
import subprocess

SERVICES = {
    "web": ("django-web-svc", "30080:80"),
    "ray": ("ray-head-svc", "30265:8265"),
    "grafana": ("grafana-svc", "30030:3000"),
    "prometheus": ("prometheus-svc", "30090:9090"),
}


def forward_command(context: str, namespace: str, service: str) -> list[str]:
    if not re.fullmatch(r"docker-desktop|kind-[a-z0-9-]+", context):
        raise ValueError("an explicit local Kubernetes context is required")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace):
        raise ValueError("invalid namespace")
    name, ports = SERVICES[service]
    return [
        "kubectl",
        "--context",
        context,
        "--namespace",
        namespace,
        "port-forward",
        "--address",
        "127.0.0.1",
        f"service/{name}",
        ports,
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--service", choices=SERVICES, required=True)
    parser.add_argument("--seconds", type=int, default=900)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    try:
        return subprocess.run(
            forward_command(args.context, args.namespace, args.service),
            timeout=args.seconds,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired:
        print("Port-forward time limit reached; forwarding stopped.")
        return 0
    except (OSError, ValueError):
        parser.exit(1, "Port-forward failed; verify the local context and service.\n")


if __name__ == "__main__":
    raise SystemExit(main())
