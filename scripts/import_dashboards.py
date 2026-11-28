#!/usr/bin/env python3
"""Import Ray Grafana dashboards into Grafana."""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana-svc:3000")
DASHBOARDS_PATH = "/tmp/ray/session_latest/metrics/grafana/dashboards"
MAX_RETRIES = 30
RETRY_DELAY = 10
MAX_DASHBOARDS = 32
MAX_DASHBOARD_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


def _validated_grafana_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("GRAFANA_URL must use http or https with a valid hostname")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("GRAFANA_URL must not embed credentials")
    return url.rstrip("/")


def _auth_header() -> str:
    username = os.environ.get("GF_SECURITY_ADMIN_USER", "").strip()
    password = os.environ.get("GF_SECURITY_ADMIN_PASSWORD", "")
    if not username or not password:
        raise ValueError("GF_SECURITY_ADMIN_USER and GF_SECURITY_ADMIN_PASSWORD are required")
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _read_bounded_regular_file(filepath: os.PathLike[str] | str) -> bytes:
    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if not hasattr(os, "O_NOFOLLOW") and os.path.islink(filepath):
        raise ValueError("dashboard path must not be a symlink")
    with open(
        filepath,
        "rb",
        buffering=0,
        opener=lambda name, mode: os.open(name, mode | flags),
    ) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("dashboard path must be a regular file")
        if st.st_size > MAX_DASHBOARD_BYTES:
            raise ValueError("dashboard exceeds import bound")
        chunks = bytearray()
        limit = MAX_DASHBOARD_BYTES + 1
        while len(chunks) < limit:
            chunk = f.read(min(64 * 1024, limit - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        encoded = bytes(chunks)
    if len(encoded) > MAX_DASHBOARD_BYTES:
        raise ValueError("dashboard exceeds import bound")
    return encoded


def wait_for_grafana() -> bool:
    """Wait for Grafana to be ready."""
    try:
        base_url = _validated_grafana_url(GRAFANA_URL)
    except ValueError as e:
        print(f"ERROR: {e}")
        return False
    print(f"Waiting for Grafana at {base_url}...")
    for i in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(f"{base_url}/api/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    print("Grafana is ready!")
                    return True
        except Exception as e:
            print(f"  Attempt {i + 1}/{MAX_RETRIES}: {type(e).__name__}")
        time.sleep(RETRY_DELAY)
    return False


def wait_for_dashboards() -> bool:
    """Wait for Ray to generate dashboard files."""
    print(f"Waiting for Ray dashboards at {DASHBOARDS_PATH}...")
    for i in range(MAX_RETRIES):
        pattern = os.path.join(DASHBOARDS_PATH, "*_dashboard.json")
        files = glob.glob(pattern)
        if files:
            print(f"Found {len(files)} dashboard files!")
            return True
        print(f"  Attempt {i + 1}/{MAX_RETRIES}: No dashboards yet...")
        time.sleep(RETRY_DELAY)
    return False


def import_dashboard(filepath: os.PathLike[str] | str) -> bool:
    """Import a single dashboard JSON file into Grafana."""
    print(f"Importing {os.path.basename(os.fspath(filepath))}...")

    try:
        base_url = _validated_grafana_url(GRAFANA_URL)
        auth_header = _auth_header()
        encoded = _read_bounded_regular_file(filepath)
        dashboard = json.loads(encoded)
        if not isinstance(dashboard, dict) or not dashboard.get("uid"):
            raise ValueError("dashboard has no identity")
    except (OSError, ValueError):
        print("  ERROR: dashboard file or configuration is invalid")
        return False

    payload = json.dumps(
        {
            "dashboard": dashboard,
            "overwrite": True,
            "folderUid": "",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/dashboards/db",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": auth_header},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            encoded = resp.read(MAX_RESPONSE_BYTES + 1)
            if resp.status != 200 or len(encoded) > MAX_RESPONSE_BYTES:
                raise ValueError("import response status or size is invalid")
        result = json.loads(encoded)
        if not isinstance(result, dict) or result.get("status") != "success":
            raise ValueError("import was not accepted")
        if result.get("uid") != dashboard["uid"]:
            raise ValueError("import response identity does not match")
        print("  OK: dashboard imported")
        return True
    except urllib.error.HTTPError as e:
        e.close()
        print(f"  HTTPError {e.code}: dashboard import refused")
        return False
    except Exception as e:
        print(f"  Error: {type(e).__name__}: dashboard import failed")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Exit after one import pass")
    args = parser.parse_args(argv)
    try:
        _validated_grafana_url(GRAFANA_URL)
        _auth_header()
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1

    if not wait_for_grafana():
        print("ERROR: Grafana not available")
        return 1

    if not wait_for_dashboards():
        print("ERROR: Ray dashboards not available")
        return 1

    pattern = os.path.join(DASHBOARDS_PATH, "*_dashboard.json")
    files = sorted(glob.glob(pattern))
    if not 1 <= len(files) <= MAX_DASHBOARDS:
        print("ERROR: dashboard inventory is empty or exceeds its bound")
        return 1

    print(f"\nImporting {len(files)} dashboards...")
    success = 0
    for filepath in files:
        if import_dashboard(filepath):
            success += 1

    print(f"\nImported {success}/{len(files)} dashboards")

    if success == len(files):
        if args.once:
            print("Dashboard import complete!")
            return 0
        print("Dashboard import complete! Sleeping to keep sidecar alive...")
        while True:
            time.sleep(3600)

    return 1


if __name__ == "__main__":
    sys.exit(main())
