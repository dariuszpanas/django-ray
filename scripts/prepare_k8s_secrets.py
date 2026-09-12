"""Prepare a private local Kubernetes credential bundle without applying it."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any

SECRET_NAMES = (
    "django-ray-runtime",
    "django-ray-secret",
    "django-ray-database",
    "django-ray-auth",
    "django-ray-bootstrap",
    "django-ray-grafana",
    "django-ray-metrics",
    "django-ray-demo",
)
SECRET_KEYS = {
    "django-ray-runtime": {"DJANGO_SECRET_KEY", "DATABASE_USER", "DATABASE_PASSWORD"},
    "django-ray-secret": {"DJANGO_API_TOKEN"},
    "django-ray-database": {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"},
    "django-ray-auth": {"RAY_AUTH_TOKEN"},
    "django-ray-bootstrap": {
        "DJANGO_BOOTSTRAP_SUPERUSER",
        "DJANGO_SUPERUSER_USERNAME",
        "DJANGO_SUPERUSER_EMAIL",
        "DJANGO_SUPERUSER_PASSWORD",
    },
    "django-ray-grafana": {"GF_SECURITY_ADMIN_USER", "GF_SECURITY_ADMIN_PASSWORD"},
    "django-ray-metrics": {"DJANGO_METRICS_TOKEN"},
    "django-ray-demo": {"DJANGO_DEMO_TOKEN"},
}


def _credential_paths(repository: Path, namespace: str) -> tuple[Path, ...]:
    paths = (repository / ".local", repository / ".local" / "k8s")
    return (*paths, paths[-1] / namespace)


def _reject_link(path: Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise ValueError("local credential paths must not be links or junctions")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential document key")
        result[key] = value
    return result


def validated_bundle(repository: Path, namespace: str) -> dict[str, object]:
    """Read only the bounded expected local Secret document, failing before kubectl."""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace):
        raise ValueError("invalid namespace")
    for directory in _credential_paths(repository, namespace):
        _reject_link(directory)
    target = _credential_paths(repository, namespace)[-1] / "secrets.json"
    _reject_link(target)
    if not target.is_file():
        raise ValueError("prepare the local credential file first")
    with target.open("rb") as stream:
        raw = stream.read(32769)
    if len(raw) > 32768:
        raise ValueError("credential document exceeds its byte limit")
    bundle = json.loads(raw, object_pairs_hook=_unique_object)
    if (
        not isinstance(bundle, dict)
        or set(bundle) != {"apiVersion", "kind", "items"}
        or bundle["apiVersion"] != "v1"
        or bundle["kind"] != "List"
        or not isinstance(bundle["items"], list)
        or len(bundle["items"]) != len(SECRET_NAMES)
    ):
        raise ValueError("invalid local Secret document")
    found = {}
    for item in bundle["items"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"apiVersion", "kind", "metadata", "type", "stringData"}
            or item["apiVersion"] != "v1"
            or item["kind"] != "Secret"
            or item["type"] != "Opaque"
            or not isinstance(item["metadata"], dict)
            or set(item["metadata"]) != {"name", "namespace"}
            or item["metadata"]["namespace"] != namespace
            or not isinstance(item["metadata"]["name"], str)
        ):
            raise ValueError("invalid local Secret resource")
        name = item["metadata"]["name"]
        data = item["stringData"]
        if (
            name not in SECRET_KEYS
            or name in found
            or not isinstance(data, dict)
            or set(data) != SECRET_KEYS[name]
        ):
            raise ValueError("local Secret names and keys must match the component allowlist")
        for key, value in data.items():
            if (
                not isinstance(value, str)
                or len(value.encode("utf-8")) > 512
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("invalid local credential value")
            minimum = 50 if key == "DJANGO_SECRET_KEY" else 32
            if any(part in key for part in ("TOKEN", "PASSWORD", "SECRET_KEY")) and (
                len(value) < minimum or len(set(value)) < 5
            ):
                raise ValueError("local credential is too short")
        found[name] = data
    runtime = found["django-ray-runtime"]
    database = found["django-ray-database"]
    if (
        runtime["DATABASE_USER"] != database["POSTGRES_USER"]
        or runtime["DATABASE_PASSWORD"] != database["POSTGRES_PASSWORD"]
    ):
        raise ValueError("database credentials must agree")
    bootstrap = found["django-ray-bootstrap"]
    if bootstrap["DJANGO_BOOTSTRAP_SUPERUSER"] not in {"true", "false"}:
        raise ValueError("bootstrap mode must be explicit")
    if bootstrap["DJANGO_BOOTSTRAP_SUPERUSER"] == "true" and (
        not re.fullmatch(r"[\w.@+-]{1,150}", bootstrap["DJANGO_SUPERUSER_USERNAME"])
        or not re.fullmatch(r"[^\s@]+@[^\s@]+", bootstrap["DJANGO_SUPERUSER_EMAIL"])
    ):
        raise ValueError("bootstrap requires an explicit identity")
    return bundle


def credential_bundle(
    namespace: str,
    *,
    bootstrap_username: str | None = None,
    bootstrap_email: str | None = None,
) -> dict[str, Any]:
    """Generate one namespace's bounded, component-scoped Secret set."""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace):
        raise ValueError("namespace must be a Kubernetes DNS label")
    if (bootstrap_username is None) != (bootstrap_email is None):
        raise ValueError("bootstrap requires both an explicit username and email")
    if bootstrap_username is not None and (
        not re.fullmatch(r"[\w.@+-]{1,150}", bootstrap_username)
        or not bootstrap_email
        or len(bootstrap_email) > 254
        or "@" not in bootstrap_email
        or any(c.isspace() for c in bootstrap_email)
    ):
        raise ValueError("bootstrap identity is invalid")
    database_password = secrets.token_urlsafe(48)
    values = (
        {
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
            "DATABASE_USER": "django_ray",
            "DATABASE_PASSWORD": database_password,
        },
        {"DJANGO_API_TOKEN": secrets.token_urlsafe(48)},
        {
            "POSTGRES_USER": "django_ray",
            "POSTGRES_PASSWORD": database_password,
            "POSTGRES_DB": "django_ray",
        },
        {"RAY_AUTH_TOKEN": secrets.token_urlsafe(48)},
        {
            "DJANGO_BOOTSTRAP_SUPERUSER": "true" if bootstrap_username else "false",
            "DJANGO_SUPERUSER_USERNAME": bootstrap_username or "",
            "DJANGO_SUPERUSER_EMAIL": bootstrap_email or "",
            "DJANGO_SUPERUSER_PASSWORD": secrets.token_urlsafe(48),
        },
        {
            "GF_SECURITY_ADMIN_USER": "local-admin",
            "GF_SECURITY_ADMIN_PASSWORD": secrets.token_urlsafe(48),
        },
        {"DJANGO_METRICS_TOKEN": secrets.token_urlsafe(48)},
        {"DJANGO_DEMO_TOKEN": secrets.token_urlsafe(48)},
    )
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": namespace},
                "type": "Opaque",
                "stringData": data,
            }
            for name, data in zip(SECRET_NAMES, values, strict=True)
        ],
    }


def prepare(
    repository: Path,
    namespace: str,
    *,
    bootstrap_username: str | None = None,
    bootstrap_email: str | None = None,
) -> Path:
    """Create an ignored private file exclusively; never rotate existing state."""
    bundle = credential_bundle(
        namespace,
        bootstrap_username=bootstrap_username,
        bootstrap_email=bootstrap_email,
    )
    directory = _credential_paths(repository, namespace)[-1]
    # Reject link traversal before creating or writing local secret material.
    for path in _credential_paths(repository, namespace):
        _reject_link(path)
        path.mkdir(mode=0o700, exist_ok=True)
    target = directory / "secrets.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(bundle, stream, indent=2)
        stream.write("\n")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--bootstrap-username")
    parser.add_argument("--bootstrap-email")
    parser.add_argument("--provision-context", help="Create missing Secrets from the existing file")
    args = parser.parse_args(argv)
    try:
        if args.provision_context:
            if args.bootstrap_username is not None or args.bootstrap_email is not None:
                raise ValueError("bootstrap identity must be supplied during preparation")
            provision(
                Path(__file__).resolve().parent.parent, args.namespace, args.provision_context
            )
            print("Local Secret set is present; existing Secrets were preserved.")
            return 0
        target = prepare(
            Path(__file__).resolve().parent.parent,
            args.namespace,
            bootstrap_username=args.bootstrap_username,
            bootstrap_email=args.bootstrap_email,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        parser.exit(
            1, "Credential preparation failed; verify the namespace and existing local files.\n"
        )
    print(f"Prepared private credentials at {target}. No Kubernetes resources were changed.")
    return 0


def provision(repository: Path, namespace: str, context: str) -> None:
    """Create an entire missing set, never apply over an existing Secret."""
    if not re.fullmatch(r"docker-desktop|kind-[a-z0-9-]+", context):
        raise ValueError("an explicit supported local context is required")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace):
        raise ValueError("invalid namespace")
    bundle = validated_bundle(repository, namespace)
    prefix = ["kubectl", "--context", context, "--namespace", namespace]
    existing = subprocess.run(
        [*prefix, "get", "secrets", *SECRET_NAMES, "--ignore-not-found", "-o", "name"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if existing.returncode:
        raise ValueError("cannot inspect the existing Secret set")
    names = set(existing.stdout.split())
    expected = {f"secret/{name}" for name in SECRET_NAMES}
    if names == expected:
        return
    if names:
        raise ValueError("partial Secret set requires explicit operator recovery")
    created = subprocess.run(
        [*prefix, "create", "-f", "-"],
        input=json.dumps(bundle),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if created.returncode:
        raise ValueError("Secret creation failed; inspect names before retrying")


if __name__ == "__main__":
    raise SystemExit(main())
