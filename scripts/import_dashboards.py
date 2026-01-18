#!/usr/bin/env python3
"""Import Ray Grafana dashboards into Grafana."""
import json
import urllib.request
import urllib.error
import os
import glob

GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana-svc:3000")
DASHBOARDS_PATH = "/tmp/ray/session_latest/metrics/grafana/dashboards"

def import_dashboard(filepath):
    """Import a single dashboard JSON file into Grafana."""
    print(f"Importing {filepath}...")

    with open(filepath) as f:
        dashboard = json.load(f)

    # Wrap for import API
    payload = json.dumps({
        "dashboard": dashboard,
        "overwrite": True,
        "folderUid": "",  # Root folder
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{GRAFANA_URL}/api/dashboards/db",
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    try:
        resp = urllib.request.urlopen(req)
        result = json.loads(resp.read().decode())
        print(f"  OK: {result.get('status', 'success')} - {result.get('slug', 'unknown')}")
        return True
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"  HTTPError {e.code}: {error_body}")
        return False
    except Exception as e:
        print(f"  Error: {type(e).__name__}: {e}")
        return False

def main():
    print(f"Grafana URL: {GRAFANA_URL}")
    print(f"Dashboards path: {DASHBOARDS_PATH}")

    # Find all dashboard JSON files
    pattern = os.path.join(DASHBOARDS_PATH, "*_dashboard.json")
    files = glob.glob(pattern)

    if not files:
        print(f"No dashboard files found matching {pattern}")
        return 1

    print(f"Found {len(files)} dashboard files")

    success = 0
    for filepath in files:
        if import_dashboard(filepath):
            success += 1

    print(f"\nImported {success}/{len(files)} dashboards")
    return 0 if success == len(files) else 1

if __name__ == "__main__":
    exit(main())

