"""Build deterministic RuntimeEnv archives for the Kubernetes sample."""

from __future__ import annotations

import argparse
import importlib.metadata
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

RECOVERY_BUNDLE_MAX_BYTES = 32 * 1024 * 1024
RECOVERY_DISTRIBUTIONS = (
    "asgiref",
    "cffi",
    "cryptography",
    "django",
    "django-ray",
    "django-unfold",
    "psycopg",
    "psycopg-binary",
    "pycparser",
    "sqlparse",
    "typing-extensions",
)
OPTIONAL_RECOVERY_DISTRIBUTIONS = ("tzdata",)
RECOVERY_REQUIRED_MEMBERS = frozenset(
    {
        "cryptography/__init__.py",
        "django/__init__.py",
        "django_ray/runtime/remote.py",
        "psycopg/__init__.py",
        "testproject/apps/cluster_tasks/workflows.py",
        "unfold/__init__.py",
    }
)
_ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class RuntimeEnvBundleError(RuntimeError):
    """Raised when the sample RuntimeEnv archive cannot be built safely."""


def _site_packages_path() -> Path:
    candidates = [
        Path(value)
        for value in sys.path
        if value and Path(value).name in {"site-packages", "dist-packages"}
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    raise RuntimeEnvBundleError("Python site-packages could not be located")


def _source_entries(source: Path, destination: PurePosixPath) -> dict[str, Path]:
    if not source.is_dir():
        raise RuntimeEnvBundleError(f"RuntimeEnv source directory is missing: {source}")
    return {
        (destination / path.relative_to(source).as_posix()).as_posix(): path
        for path in source.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    }


def _distribution_entries(
    name: str,
    *,
    site_packages: Path,
    optional: bool = False,
) -> dict[str, Path]:
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError as error:
        if optional:
            return {}
        raise RuntimeEnvBundleError(
            f"Required recovery RuntimeEnv distribution is missing: {name}"
        ) from error

    entries: dict[str, Path] = {}
    for installed_path in distribution.files or ():
        source = Path(distribution.locate_file(installed_path))
        try:
            destination = source.resolve().relative_to(site_packages)
        except (OSError, ValueError):
            # Console scripts and editable source pointers can escape
            # site-packages. The tracked source tree is added separately.
            continue
        if (
            not source.is_file()
            or "__pycache__" in destination.parts
            or source.suffix in {".pyc", ".pyo", ".pth"}
        ):
            continue
        entries[PurePosixPath(destination.as_posix()).as_posix()] = source
    if not entries:
        raise RuntimeEnvBundleError(
            f"Required recovery RuntimeEnv distribution has no package files: {name}"
        )
    return entries


def _merge_entries(*collections: Mapping[str, Path]) -> dict[str, Path]:
    entries: dict[str, Path] = {}
    for collection in collections:
        for destination, source in collection.items():
            if destination.startswith("/") or ".." in PurePosixPath(destination).parts:
                raise RuntimeEnvBundleError(
                    f"RuntimeEnv archive member escaped its root: {destination}"
                )
            previous = entries.get(destination)
            if previous is not None and previous.resolve() != source.resolve():
                raise RuntimeEnvBundleError(
                    f"RuntimeEnv archive member has conflicting sources: {destination}"
                )
            entries[destination] = source
    return entries


def _write_deterministic_archive(target: Path, entries: Mapping[str, Path]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED, compresslevel=9) as archive:
            for destination in sorted(entries):
                info = ZipInfo(destination, date_time=_ARCHIVE_TIMESTAMP)
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(
                    info,
                    entries[destination].read_bytes(),
                    compress_type=ZIP_DEFLATED,
                    compresslevel=9,
                )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def build_source_bundle(*, base: Path, target: Path) -> None:
    """Build the generic project's source-only RuntimeEnv archive."""
    entries = _merge_entries(
        _source_entries(base / "src", PurePosixPath("src")),
        _source_entries(base / "testproject", PurePosixPath("testproject")),
    )
    _write_deterministic_archive(target, entries)


def build_recovery_bundle(
    *,
    base: Path,
    target: Path,
    site_packages: Path | None = None,
    distributions: Iterable[str] = RECOVERY_DISTRIBUTIONS,
    optional_distributions: Iterable[str] = OPTIONAL_RECOVERY_DISTRIBUTIONS,
) -> None:
    """Build a self-contained archive for retrying on stock Ray images."""
    package_root = (site_packages or _site_packages_path()).resolve()
    collections = [
        _source_entries(base / "src" / "django_ray", PurePosixPath("django_ray")),
        _source_entries(base / "testproject", PurePosixPath("testproject")),
    ]
    collections.extend(
        _distribution_entries(name, site_packages=package_root) for name in distributions
    )
    collections.extend(
        _distribution_entries(
            name,
            site_packages=package_root,
            optional=True,
        )
        for name in optional_distributions
    )
    entries = _merge_entries(*collections)
    missing = sorted(RECOVERY_REQUIRED_MEMBERS - set(entries))
    if missing:
        raise RuntimeEnvBundleError(
            f"Recovery RuntimeEnv archive is missing required members: {missing}"
        )
    _write_deterministic_archive(target, entries)
    size = target.stat().st_size
    if size > RECOVERY_BUNDLE_MAX_BYTES:
        target.unlink(missing_ok=True)
        raise RuntimeEnvBundleError(
            "Recovery RuntimeEnv archive exceeds the workflow identity limit: "
            f"{size} > {RECOVERY_BUNDLE_MAX_BYTES} bytes"
        )


def main(argv: list[str] | None = None) -> int:
    """Build both sample archives and print bounded setup evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--source-target", type=Path, required=True)
    parser.add_argument("--recovery-target", type=Path, required=True)
    args = parser.parse_args(argv)

    build_source_bundle(base=args.base, target=args.source_target)
    build_recovery_bundle(base=args.base, target=args.recovery_target)
    print(
        f"RuntimeEnv bundle ready: {args.source_target} ({args.source_target.stat().st_size} bytes)"
    )
    print(
        f"Recovery RuntimeEnv bundle ready: {args.recovery_target} "
        f"({args.recovery_target.stat().st_size} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
