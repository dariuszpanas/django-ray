"""Check Python 3.15 preparation without claiming an operational Ray runtime.

Install a wheel with --no-deps in a clean Python 3.15 environment, together
with the requirements emitted by --write-requirements. The deliberate absence
of Ray is enforced throughout this resource-free compatibility selection.
"""

from __future__ import annotations

import argparse
import importlib.abc
import importlib.metadata
import importlib.util
import os
import re
import sys
import sysconfig
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SMOKE_TESTS = (
    "tests/unit/test_serialization.py",
    "tests/unit/test_import_utils.py",
    "tests/unit/test_compiled_graph_probe.py::test_payload_decode_rejects_invalid_encoding_and_non_object",
    "tests/unit/test_compiled_graph_probe.py::test_child_command_round_trips_request",
    "tests/unit/test_compiled_graph_probe.py::test_child_payload_requires_base64_padding",
)
COMPATIBILITY_TESTS = (
    *SMOKE_TESTS,
    "tests/unit/test_retry.py",
    "tests/unit/test_redaction_patterns.py",
    "tests/unit/test_runtime_env_encryption.py",
    "tests/unit/test_workflow_output_previews.py",
    "tests/unit/test_workflow_progress_protocol.py::test_canonical_round_trip_for_every_event_kind",
    "tests/unit/test_workflow_progress_protocol.py::test_decode_rejects_each_complete_run_fence_mismatch",
    "tests/unit/test_target_execution_codec.py::test_protocol_1_request_bytes_remain_unchanged",
    "tests/unit/test_target_execution_codec.py::test_target_request_round_trips_exact_canonical_evidence",
    "tests/unit/test_target_execution_codec.py::test_target_result_variants_are_disjoint_and_prove_invocation",
)


def _project() -> dict[str, Any]:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def _requirement_name(value: str) -> str:
    return re.split(r"[\[<>=!~;\s]", value, maxsplit=1)[0].lower().replace("_", "-")


def compatibility_requirements() -> list[str]:
    """Reuse the project floors while omitting only the unavailable Ray wheel."""
    project = _project()
    dependencies = project["project"]["dependencies"]
    if sum(_requirement_name(value) == "ray" for value in dependencies) != 1:
        raise RuntimeError("Expected exactly one mandatory Ray dependency")
    requirements = [value for value in dependencies if _requirement_name(value) != "ray"]
    for name in ("pytest", "pytest-django"):
        matches = [
            value
            for value in project["dependency-groups"]["dev"]
            if _requirement_name(value) == name
        ]
        if len(matches) != 1:
            raise RuntimeError(f"Expected exactly one {name} development requirement")
        requirements.extend(matches)
    return requirements


class _ForbidRay(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self.attempts: list[str] = []

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname == "ray" or fullname.startswith("ray."):
            self.attempts.append(fullname)
            raise AssertionError(f"Python 3.15 compatibility attempted a Ray import: {fullname}")
        return None


def _require_no_ray() -> None:
    if any(name == "ray" or name.startswith("ray.") for name in sys.modules):
        raise RuntimeError("Ray or a Ray stub was already imported")
    if importlib.util.find_spec("ray") is not None:
        raise RuntimeError("Use a clean environment without Ray installed")
    try:
        importlib.metadata.distribution("ray")
    except importlib.metadata.PackageNotFoundError:
        return
    raise RuntimeError("Ray distribution metadata must be absent")


def _verify_installed_source(dist_dir: Path) -> None:
    from scripts.verify_wheel import verify_distribution_archives

    project = _project()["project"]
    verify_distribution_archives(dist_dir, project["version"])
    distribution = importlib.metadata.distribution("django-ray")
    if distribution.version != project["version"]:
        raise RuntimeError("Installed django-ray version does not match the checkout")
    if "Programming Language :: Python :: 3.15" in distribution.metadata.get_all("Classifier", []):
        raise RuntimeError("Preparatory checks must not advertise full Python 3.15 support")
    if distribution.read_text("direct_url.json"):
        import json

        direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
        if direct_url.get("dir_info", {}).get("editable"):
            raise RuntimeError("Install the built wheel, not an editable checkout")

    import django_ray

    if django_ray.__file__ is None:
        raise RuntimeError("The installed django-ray package has no concrete file")
    package_root = Path(django_ray.__file__).resolve().parent
    if package_root != Path(distribution.locate_file("django_ray")).resolve():
        raise RuntimeError("Imported django-ray does not belong to the installed distribution")
    if package_root.is_relative_to(ROOT / "src") or not package_root.is_relative_to(sys.prefix):
        raise RuntimeError("django-ray must import from this environment's installed wheel")
    source_root = ROOT / "src" / "django_ray"
    source_files = {path.relative_to(source_root) for path in source_root.rglob("*.py")}
    installed_files = {path.relative_to(package_root) for path in package_root.rglob("*.py")}
    if source_files != installed_files:
        raise RuntimeError("Installed wheel Python files differ from the checkout")
    for relative in sorted(source_files):
        if (source_root / relative).read_bytes() != (package_root / relative).read_bytes():
            raise RuntimeError(f"Installed wheel source differs from checkout: {relative}")
    print(f"Verified installed wheel source: {len(source_files)} Python files", flush=True)


def _compile_source() -> None:
    count = 0
    for directory in ("src", "scripts", "tests", "testproject"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
            count += 1
    print(f"Compiled {count} source files on Python {sys.version.split()[0]}", flush=True)


def _run_tests(*, smoke: bool, blocker: _ForbidRay) -> int:
    import pytest

    class CompatibilityEvidence:
        @pytest.hookimpl(tryfirst=True)
        def pytest_collectreport(self, report: Any) -> None:
            if report.skipped:
                raise pytest.UsageError("Compatibility collection must not skip selected tests")

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item: Any, call: Any) -> Any:
            del item, call
            outcome = yield
            report = outcome.get_result()
            if report.skipped or hasattr(report, "wasxfail"):
                report.outcome = "failed"
                report.longrepr = (
                    "Compatibility evidence must execute and pass; skip/xfail is forbidden"
                )
                if hasattr(report, "wasxfail"):
                    del report.wasxfail

        def pytest_collection_modifyitems(self, items: list[Any]) -> None:
            external_fixtures = {
                "db",
                "transactional_db",
                "django_db_reset_sequences",
                "django_db_serialized_rollback",
                "live_server",
                "ray_cluster",
                "live_ray_cluster",
            }
            for item in items:
                if any(
                    item.get_closest_marker(marker) is not None
                    for marker in ("real_ray", "live_cluster", "postgresql", "django_db")
                ) or external_fixtures.intersection(item.fixturenames):
                    raise pytest.UsageError(
                        f"Compatibility selection must be resource-free: {item.nodeid}"
                    )

    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ.pop("PYTEST_ADDOPTS", None)
    os.environ.pop("PYTEST_PLUGINS", None)
    os.environ.pop("DJANGO_SETTINGS_MODULE", None)
    result = pytest.main(
        [
            "-p",
            "pytest_django.plugin",
            "-o",
            "addopts=",
            "-q",
            "-W",
            "error::DeprecationWarning",
            "-W",
            "error::PendingDeprecationWarning",
            *(SMOKE_TESTS if smoke else COMPATIBILITY_TESTS),
        ],
        plugins=[CompatibilityEvidence()],
    )
    if blocker.attempts:
        raise RuntimeError(f"Ray imports were attempted: {sorted(set(blocker.attempts))}")
    sys.meta_path.remove(blocker)
    _require_no_ray()
    return int(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-requirements", type=Path)
    parser.add_argument("--dist-dir", type=Path)
    parser.add_argument(
        "--smoke", action="store_true", help="Run only the focused host-safe smoke tests"
    )
    arguments = parser.parse_args(argv)
    if arguments.write_requirements is not None:
        arguments.write_requirements.write_text(
            "\n".join(compatibility_requirements()) + "\n", encoding="utf-8"
        )
        return 0
    if arguments.dist_dir is None:
        parser.error("--dist-dir is required for validation")
    if sys.version_info[:2] != (3, 15):
        parser.error("validation requires the standard CPython 3.15 interpreter")
    if sys.implementation.name != "cpython" or sys.version_info.releaselevel != "final":
        parser.error("validation requires a released CPython 3.15 interpreter")
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        parser.error("free-threaded Python is outside this compatibility lane")
    if not arguments.smoke and sys.platform != "linux":
        parser.error("the full compatibility selection requires bounded Linux; use --smoke locally")
    _require_no_ray()
    blocker = _ForbidRay()
    sys.meta_path.insert(0, blocker)
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    _compile_source()
    _verify_installed_source(arguments.dist_dir.resolve())
    result = _run_tests(smoke=arguments.smoke, blocker=blocker)
    if result == 0:
        print("Python 3.15 preparation passed without Ray; runtime support remains unqualified.")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
