"""Export two exact source epochs and a build plan, without building images.

Run from the clean candidate checkout after the qualifier has been committed.
The Python image digest and patch are reviewed caller inputs, not authenticated
image contents. A successful export is neither image nor native qualification.
Failed exports retain their newly created directory and cannot be reused.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from qualification.upgrade.contract import BASELINE_COMMIT, BASELINE_VERSION, CANDIDATE_VERSION

ROOT = Path(__file__).resolve().parents[2]
_MAX_ARCHIVE = 128 * 1024 * 1024
_MAX_CONTENT = 256 * 1024 * 1024
_MAX_METADATA = 4 * 1024 * 1024
_MAX_MEMBERS = 10000
_METADATA = ("pyproject.toml", "uv.lock", "src/django_ray/__init__.py")
_DOCKERFILE = "qualification/upgrade/RuntimeDockerfile"
_OVERLAY = (
    "qualification/__init__.py",
    "qualification/docker/__init__.py",
    "qualification/docker/scenario.py",
    "qualification/upgrade/__init__.py",
    "qualification/upgrade/contract.py",
    "qualification/upgrade/runtime_contract.py",
    "qualification/upgrade/runtime_tasks.py",
    "qualification/upgrade/runtime_settings.py",
    "qualification/upgrade/runtime_steps.py",
    "qualification/upgrade/runtime_history.py",
    "qualification/upgrade/runtime_history_settings.py",
    "qualification/upgrade/runtime_artifacts.py",
    "qualification/upgrade/runtime_database.py",
    "qualification/upgrade/runtime_restore.py",
    "qualification/upgrade/runtime_scratch.py",
)


class RuntimeSourceError(ValueError):
    """A fixed refusal without command stderr or caller-provided content."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise RuntimeSourceError(reason)


def _git(*arguments: str, output: BinaryIO | None = None) -> bytes:
    # Disable ambient Git redirection/replacements and external status helpers.
    # Repository configuration remains readable, but these commands never write
    # the index, invoke a shell, fetch, check out, or run an archive helper.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
    )
    try:
        result = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.attributesFile={os.devnull}",
                *arguments,
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE if output is None else output,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30 if arguments[0] == "archive" else 10,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeSourceError("source-git-command-failed") from None
    if output is not None:
        return b""
    _require(
        type(result.stdout) is bytes and len(result.stdout) <= _MAX_METADATA,
        "source-git-output-too-large",
    )
    return result.stdout


def _oid(expression: str) -> str:
    value = _git("rev-parse", "--verify", expression).strip()
    _require(re.fullmatch(rb"[0-9a-f]{40}", value) is not None, "invalid-source-object")
    return value.decode("ascii")


def _clean_head() -> tuple[str, str]:
    _require(
        _git("status", "--porcelain=v1", "-z", "--untracked-files=all") == b"",
        "candidate-checkout-must-be-clean",
    )
    commit = _oid("HEAD^{commit}")
    return commit, _oid(f"{commit}^{{tree}}")


def _path(value: str) -> bool:
    return (
        bool(value)
        and "\\" not in value
        and ":" not in value
        and "\0" not in value
        and not PurePosixPath(value).is_absolute()
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _tree_files(commit: str) -> dict[str, str]:
    body = _git("ls-tree", "-r", "-z", "--full-tree", commit)
    _require(body.endswith(b"\0"), "invalid-source-tree")
    entries = body[:-1].split(b"\0")
    _require(0 < len(entries) <= _MAX_MEMBERS, "source-tree-too-large")
    result = {}
    for entry in entries:
        match = re.fullmatch(rb"100(?:644|755) blob ([0-9a-f]{40})\t(.+)", entry)
        _require(match is not None, "unsupported-source-tree-entry")
        assert match is not None
        try:
            name = match[2].decode("utf-8")
        except UnicodeError:
            raise RuntimeSourceError("unsupported-source-tree-entry") from None
        _require(_path(name) and name not in result, "unsupported-source-tree-entry")
        result[name] = match[1].decode("ascii")
    return result


def _validate_metadata(contents: dict[str, bytes], build: str) -> None:
    package, ray = (
        (BASELINE_VERSION, "2.56.0") if build == "baseline" else (CANDIDATE_VERSION, "2.58.0")
    )
    try:
        project = tomllib.loads(contents["pyproject.toml"].decode("utf-8"))["project"]
        lock = tomllib.loads(contents["uv.lock"].decode("utf-8"))
        packages = lock["package"]
        ray_entries = [entry for entry in packages if entry["name"] == "ray"]
        own = [entry for entry in packages if entry["name"] == "django-ray"]
        versions = [
            node.value.value
            for node in ast.parse(contents["src/django_ray/__init__.py"]).body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ]
        valid = (
            project["name"] == "django-ray"
            and project["version"] == package
            and project["requires-python"] == lock["requires-python"] == ">=3.12"
            and f"ray[default]>={ray}" in project["dependencies"]
            and type(lock["version"]) is int
            and lock["version"] == 1
            and len(ray_entries) == 1
            and ray_entries[0]["version"] == ray
            and ray_entries[0]["source"] == {"registry": "https://pypi.org/simple"}
            and len(own) == 1
            and own[0]["version"] == package
            and own[0]["source"] == {"editable": "."}
            and [entry for entry in own[0]["metadata"]["requires-dist"] if entry["name"] == "ray"]
            == [{"name": "ray", "extras": ["default"], "specifier": f">={ray}"}]
            and versions == [package]
        )
    except (KeyError, TypeError, ValueError, SyntaxError):
        raise RuntimeSourceError("source-epoch-metadata-refused") from None
    _require(valid, "source-epoch-metadata-refused")


def _archive(path: Path, commit: str, files: dict[str, str]) -> dict[str, bytes]:
    with path.open("xb") as output:
        _git("archive", "--format=tar", commit, output=output)
    _require(0 < path.stat().st_size <= _MAX_ARCHIVE, "source-archive-too-large")
    captured = {}
    seen = set()
    names = set()
    total = 0
    try:
        with tarfile.open(path, mode="r:") as archive:
            _require(archive.pax_headers.get("comment") == commit, "source-archive-commit-mismatch")
            for index, member in enumerate(archive, start=1):
                name = member.name.rstrip("/") if member.isdir() else member.name
                total += member.size
                _require(
                    index <= _MAX_MEMBERS
                    and total <= _MAX_CONTENT
                    and _path(name)
                    and name not in names
                    and (member.isdir() or member.isfile()),
                    "unsupported-source-archive-entry",
                )
                names.add(name)
                if member.isdir():
                    continue
                _require(name in files, "source-archive-tree-mismatch")
                digest = hashlib.sha1(
                    f"blob {member.size}\0".encode("ascii"), usedforsecurity=False
                )
                stream = archive.extractfile(member)
                assert stream is not None
                with stream:
                    if name in (*_METADATA, _DOCKERFILE):
                        _require(member.size <= _MAX_METADATA, "source-metadata-too-large")
                        captured[name] = stream.read()
                        digest.update(captured[name])
                    else:
                        while chunk := stream.read(1024 * 1024):
                            digest.update(chunk)
                _require(digest.hexdigest() == files[name], "source-archive-tree-mismatch")
                seen.add(name)
    except (tarfile.TarError, OSError, UnicodeError):
        raise RuntimeSourceError("source-archive-unreadable") from None
    _require(seen == set(files), "source-archive-tree-mismatch")
    return captured


def _image(value: str, python_version: str) -> None:
    _require(
        type(value) is str
        and len(value) <= 512
        and re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}", value) is not None,
        "expected-reviewed-digest-pinned-python-image",
    )
    _require(
        type(python_version) is str
        and re.fullmatch(r"3\.(12|13|14)\.(0|[1-9][0-9]{0,2})", python_version) is not None,
        "expected-exact-python-patch",
    )


def prepare_runtime_sources(destination: Path, *, python_image: str, python_version: str) -> dict:
    """Create a new export outside this checkout; never execute the build plan.

    Only the exact clean HEAD supplies qualifier code and the Dockerfile. The
    complete archive file set and every Git blob are checked, so local export
    attributes cannot silently omit or substitute source. Lock consistency is
    bounded metadata validation; the image build must still use ``uv --frozen``
    and independently verify its installed wheel, Python patch and Ray version.
    """
    _image(python_image, python_version)
    _require(isinstance(destination, Path), "expected-new-source-directory")
    _require(not destination.is_symlink(), "expected-new-source-directory")
    destination = destination.resolve()
    _require(
        not destination.exists()
        and not destination.is_relative_to(ROOT.resolve())
        and destination.parent.is_dir()
        and not any(character in str(destination) for character in "\0\r\n,"),
        "expected-new-source-directory",
    )
    candidate, candidate_tree = _clean_head()
    _require(_oid("v0.4.0^{commit}") == BASELINE_COMMIT, "released-baseline-commit-mismatch")
    baseline_tree = _oid(f"{BASELINE_COMMIT}^{{tree}}")
    _require(
        candidate != BASELINE_COMMIT and candidate_tree != baseline_tree,
        "candidate-must-be-distinct",
    )
    epochs = {
        "baseline": (BASELINE_COMMIT, baseline_tree),
        "candidate": (candidate, candidate_tree),
    }
    trees = {}
    metadata = {}
    for build, (commit, _tree) in epochs.items():
        trees[build] = _tree_files(commit)
        required = set(_METADATA) | (
            set(_OVERLAY) | {_DOCKERFILE} if build == "candidate" else set()
        )
        _require(required <= set(trees[build]), "required-source-file-missing")
        metadata[build] = {name: _git("show", f"{commit}:{name}") for name in _METADATA}
        _validate_metadata(metadata[build], build)
    _require(_clean_head() == (candidate, candidate_tree), "candidate-changed-during-export")
    destination.mkdir(mode=0o755)  # Exclusive mkdir; a racing existing directory is never reused.
    sources = {}
    dockerfile = b""
    for build, (commit, tree) in epochs.items():
        context = destination / build
        context.mkdir(mode=0o755)
        path = context / "source.tar"
        captured = _archive(path, commit, trees[build])
        _require(
            all(captured.get(name) == value for name, value in metadata[build].items()),
            "source-archive-metadata-mismatch",
        )
        with path.open("rb") as stream:
            archive_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        sources[build] = {
            "commit": commit,
            "tree": tree,
            "archive": str(path),
            "archive_sha256": archive_digest,
            "lock_sha256": hashlib.sha256(captured["uv.lock"]).hexdigest(),
            "package_version": BASELINE_VERSION if build == "baseline" else CANDIDATE_VERSION,
            "ray_version": "2.56.0" if build == "baseline" else "2.58.0",
        }
        if build == "candidate":
            dockerfile = captured[_DOCKERFILE]
    _require(_clean_head() == (candidate, candidate_tree), "candidate-changed-during-export")
    _require(_oid("v0.4.0^{commit}") == BASELINE_COMMIT, "released-baseline-commit-mismatch")
    with (destination / "RuntimeDockerfile").open("xb") as stream:
        stream.write(dockerfile)
    plans = {}
    for build, source in sources.items():
        arguments = {
            "PYTHON_IMAGE": python_image,
            "RUNTIME_BUILD": build,
            "EXPECTED_PYTHON_VERSION": python_version,
            "EPOCH_SOURCE_COMMIT": source["commit"],
            "EPOCH_SOURCE_SHA256": source["archive_sha256"],
            "QUALIFIER_SOURCE_COMMIT": candidate,
            "QUALIFIER_SOURCE_SHA256": sources["candidate"]["archive_sha256"],
        }
        plans[build] = [
            "docker",
            "buildx",
            "build",
            "--file",
            str(destination / "RuntimeDockerfile"),
            "--build-context",
            f"epoch-source={destination / build}",
            "--build-context",
            f"qualifier-source={destination / 'candidate'}",
            *[
                argument
                for name, value in arguments.items()
                for argument in ("--build-arg", f"{name}={value}")
            ],
            "--output",
            f"type=oci,dest={destination / (build + '.oci.tar')}",
            str(destination / build),
        ]
    result = {
        "schema": 1,
        "kind": "native-upgrade-source-export",
        "python_image": python_image,
        "python_version": python_version,
        "sources": sources,
        "dockerfile_sha256": hashlib.sha256(dockerfile).hexdigest(),
        "build_argv": plans,
        "complete_upgrade_gate": False,
    }
    with (destination / "source-plan.json").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--python-image", required=True)
    parser.add_argument("--python-version", required=True)
    arguments = parser.parse_args()
    try:
        prepare_runtime_sources(
            arguments.destination,
            python_image=arguments.python_image,
            python_version=arguments.python_version,
        )
    except (RuntimeSourceError, OSError):
        raise SystemExit(
            "runtime-source-export-refused; partial directory, if any, was retained"
        ) from None
    print("runtime-source-exported; image-build-and-native-qualification-not-run")


if __name__ == "__main__":
    main()
