"""Seal clean committed source and verify its bytes without a Git installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Any, cast

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from scripts.test_suite_taxonomy import InventoryError  # noqa: E402

DEFAULT_MANIFEST = Path(".github/test-suite-taxonomy.json")
GENERATED_BASELINE_RE = re.compile(
    r"^docs/investigations/test-suite-baseline-\d{4}-\d{2}-\d{2}\.(?:json|md)$"
)
BINARY_SUFFIXES = frozenset(
    {".gif", ".gz", ".ico", ".jpeg", ".jpg", ".pdf", ".png", ".whl", ".zip"}
)
DIGEST_ROOTS = [
    "git ls-files --cached --others --exclude-standard",
    "excluding generated test-suite baseline JSON and Markdown",
]
# These are tool outputs, not source. Evidence itself belongs outside the archive.
GENERATED_ROOT_DIRECTORIES = frozenset(
    {
        ".git",
        ".cache",
        ".venv",
        "node_modules",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".ty_cache",
        ".pyright",
        "site",
        "dist",
        "build",
        "htmlcov",
    }
)
GENERATED_ROOT_FILES = frozenset({".git", ".coverage", "coverage.xml", "db.sqlite3"})
_BOUND_SOURCE: ContextVar[tuple[Path, Path] | None] = ContextVar("test_suite_source", default=None)


@contextmanager
def bound_source(root: Path, source_manifest: Path | None) -> Iterator[None]:
    """Bind explicit archive provenance for nested helpers on this exact root only."""
    if source_manifest is None:
        yield
        return
    token = _BOUND_SOURCE.set((root.resolve(), source_manifest.resolve()))
    try:
        yield
    finally:
        _BOUND_SOURCE.reset(token)


def _git(root: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    try:
        return subprocess.run(
            ["git", *arguments], cwd=root, input=input_bytes, check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise InventoryError("cannot read Git source identity") from error


def _canonical_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 for character in value)
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", "..", ".git"} for part in value.split("/"))
    ):
        raise InventoryError("source inventory paths must be canonical relative file paths")
    return value


def _content_digest(relative: str, content: bytes) -> bytes:
    if Path(relative).suffix.lower() not in BINARY_SUFFIXES and b"\0" not in content:
        content = content.replace(b"\r\n", b"\n")
    return hashlib.sha256(content).digest()


def _digest_record(contents: dict[str, bytes | None]) -> dict[str, object]:
    digest = hashlib.sha256()
    paths = sorted(path for path in contents if not GENERATED_BASELINE_RE.fullmatch(path))
    for relative in paths:
        digest.update(relative.encode("utf-8") + b"\0")
        content = contents[relative]
        digest.update(b"missing\0" if content is None else b"file\0")
        if content is not None:
            digest.update(_content_digest(relative, content))
    return {
        "algorithm": "sha256",
        "digest": digest.hexdigest(),
        "file_count": len(paths),
        "roots": DIGEST_ROOTS.copy(),
    }


def source_digest(
    root: Path, manifest_path: Path, source_manifest: Path | None = None
) -> dict[str, object]:
    """Return the existing taxonomy identity, or verify a sealed archive first."""
    root = root.resolve()
    binding = _BOUND_SOURCE.get()
    if source_manifest is None and binding is not None and binding[0] == root:
        source_manifest = binding[1]
    try:
        manifest_relative = manifest_path.resolve().relative_to(root).as_posix()
    except ValueError as error:
        raise InventoryError("taxonomy manifest must stay inside the repository") from error
    if source_manifest is not None:
        sealed = verify_source(root, source_manifest)
        if sealed["taxonomy_manifest"] != manifest_relative:
            raise InventoryError("sealed source uses another taxonomy manifest")
        return cast(dict[str, object], sealed["source_digest"])
    paths = sorted(
        {
            os.fsdecode(raw_path).replace("\\", "/")
            for raw_path in _git(
                root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            ).split(b"\0")
            if raw_path
        }
        | {manifest_relative}
    )
    # Keep ordinary checkout semantics, including missing and non-file inputs.
    digest = hashlib.sha256()
    paths = [path for path in paths if not GENERATED_BASELINE_RE.fullmatch(path)]
    for relative in paths:
        path = root / relative
        digest.update(relative.encode("utf-8") + b"\0")
        if not path.exists():
            digest.update(b"missing\0")
        elif not path.is_file():
            digest.update(b"non-file\0")
        else:
            try:
                content = path.read_bytes()
            except OSError as error:
                raise InventoryError(f"cannot hash taxonomy source input {path}") from error
            digest.update(b"file\0")
            digest.update(_content_digest(relative, content))
    return {
        "algorithm": "sha256",
        "digest": digest.hexdigest(),
        "file_count": len(paths),
        "roots": DIGEST_ROOTS.copy(),
    }


def _clean_identity(root: Path) -> tuple[str, str]:
    if Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root:
        raise InventoryError("source sealing must use the Git repository root")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise InventoryError("source sealing requires a clean Git checkout")
    identity = _git(root, "rev-parse", "HEAD", "HEAD^{tree}").decode("ascii").splitlines()
    return identity[0], identity[1]


def seal_source(root: Path, output_path: Path) -> dict[str, Any]:
    """Record every committed file; the output must be outside the source tree."""
    root = root.resolve()
    output_path = output_path.resolve()
    if output_path.is_relative_to(root):
        raise InventoryError("source manifest output must stay outside the source tree")
    before = _clean_identity(root)
    entries: list[tuple[str, str, str]] = []
    names: set[str] = set()
    for raw_entry in _git(root, "ls-tree", "-r", "-z", before[0]).split(b"\0"):
        if not raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split()
        relative = _canonical_path(raw_path.decode("utf-8"))
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise InventoryError("sealed source supports regular files only")
        if relative.casefold() in names:
            raise InventoryError("source inventory paths must be case-insensitively unique")
        names.add(relative.casefold())
        entries.append((relative, mode, object_id))
    blobs = _git(
        root,
        "cat-file",
        "--batch",
        input_bytes="".join(f"{item[2]}\n" for item in entries).encode(),
    )
    offset = 0
    contents: dict[str, bytes | None] = {}
    files: list[dict[str, object]] = []
    for relative, mode, object_id in entries:
        end = blobs.index(b"\n", offset)
        header = blobs[offset:end].decode("ascii").split()
        if len(header) != 3 or header[:2] != [object_id, "blob"]:
            raise InventoryError("Git returned an inconsistent source blob")
        size = int(header[2])
        content = blobs[end + 1 : end + 1 + size]
        offset = end + size + 2
        if len(content) != size or blobs[offset - 1 : offset] != b"\n":
            raise InventoryError("Git returned an incomplete source blob")
        contents[relative] = content
        files.append(
            {
                "path": relative,
                "mode": mode,
                "size": size,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    if DEFAULT_MANIFEST.as_posix() not in contents:
        raise InventoryError("committed source has no taxonomy manifest")
    sealed = {
        "schema_version": 1,
        "source_commit": before[0],
        "source_tree": before[1],
        "taxonomy_manifest": DEFAULT_MANIFEST.as_posix(),
        "source_digest": _digest_record(contents),
        "files": sorted(files, key=lambda item: str(item["path"])),
    }
    if _clean_identity(root) != before:
        raise InventoryError("Git source identity changed while sealing")
    try:
        with output_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(sealed, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except OSError as error:
        raise InventoryError("cannot create source manifest output") from error
    return sealed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InventoryError("source manifest contains duplicate JSON keys")
        result[key] = value
    return result


def _tree_identity(
    files: list[dict[str, Any]], contents: dict[str, bytes | None], length: int
) -> str:
    """Reconstruct Git's tree object so omitted inventory entries cannot pass."""

    def object_digest(kind: str, content: bytes) -> bytes:
        value = f"{kind} {len(content)}\0".encode() + content
        if length == 40:
            return hashlib.sha1(value, usedforsecurity=False).digest()
        return hashlib.sha256(value).digest()

    tree: dict[str, Any] = {}
    for entry in files:
        parts = entry["path"].split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise InventoryError("source inventory has a file/directory collision")
        if parts[-1] in node:
            raise InventoryError("source inventory has a file/directory collision")
        node[parts[-1]] = (entry["mode"], contents[entry["path"]])

    def encode_tree(node: dict[str, Any]) -> bytes:
        records = []
        for name, value in node.items():
            directory = isinstance(value, dict)
            mode = "40000" if directory else value[0]
            digest = encode_tree(value) if directory else object_digest("blob", value[1])
            name_bytes = name.encode("utf-8")
            records.append(
                (
                    name_bytes + (b"/" if directory else b""),
                    mode.encode() + b" " + name_bytes + b"\0" + digest,
                )
            )
        return object_digest("tree", b"".join(record for _, record in sorted(records)))

    return encode_tree(tree).hex()


def verify_source(root: Path, source_manifest: Path) -> dict[str, Any]:
    """Verify exact source bytes and complete file membership without calling Git."""
    root = root.resolve()
    try:
        sealed = json.loads(
            source_manifest.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, ValueError) as error:
        raise InventoryError("cannot read source manifest") from error
    expected_keys = {
        "schema_version",
        "source_commit",
        "source_tree",
        "taxonomy_manifest",
        "source_digest",
        "files",
    }
    if (
        not isinstance(sealed, dict)
        or set(sealed) != expected_keys
        or type(sealed["schema_version"]) is not int
        or sealed["schema_version"] != 1
    ):
        raise InventoryError("unsupported source manifest")
    for key in ("source_commit", "source_tree"):
        if not isinstance(sealed[key], str) or not re.fullmatch(
            r"[a-f0-9]{40}|[a-f0-9]{64}", sealed[key]
        ):
            raise InventoryError("source manifest needs full Git identities")
    taxonomy = _canonical_path(sealed["taxonomy_manifest"])
    if not isinstance(sealed["files"], list) or not sealed["files"]:
        raise InventoryError("source manifest needs a complete file inventory")
    contents: dict[str, bytes | None] = {}
    names: set[str] = set()
    for entry in sealed["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "mode", "size", "sha256"}:
            raise InventoryError("invalid source file inventory entry")
        relative = _canonical_path(entry["path"])
        if relative.casefold() in names:
            raise InventoryError("source inventory paths must be case-insensitively unique")
        names.add(relative.casefold())
        if (
            entry["mode"] not in {"100644", "100755"}
            or type(entry["size"]) is not int
            or entry["size"] < 0
        ):
            raise InventoryError("invalid source file mode or size")
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise InventoryError(f"source file is missing or unsafe: {relative}")
        try:
            content = path.read_bytes()
        except OSError as error:
            raise InventoryError(f"cannot read source file: {relative}") from error
        if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise InventoryError(f"source file bytes changed: {relative}")
        if os.name != "nt" and bool(path.stat().st_mode & 0o111) != (entry["mode"] == "100755"):
            raise InventoryError(f"source file executable mode changed: {relative}")
        contents[relative] = content
    observed: set[str] = set()
    for directory, children, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        children[:] = [
            name
            for name in children
            if not (
                name == "__pycache__"
                or (
                    current == root
                    and (name in GENERATED_ROOT_DIRECTORIES or name.endswith(".egg-info"))
                )
                or (current == root / "docs" and name == "_build")
            )
            or any(
                path.startswith((current / name).relative_to(root).as_posix() + "/")
                for path in contents
            )
        ]
        filenames = [
            name
            for name in filenames
            if not (current == root and name in GENERATED_ROOT_FILES and name not in contents)
        ]
        for name in children + filenames:
            path = current / name
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root):
                raise InventoryError("source archive contains an unsafe filesystem entry")
        observed.update((current / name).relative_to(root).as_posix() for name in filenames)
    if observed != set(contents):
        raise InventoryError("source archive file inventory does not match the sealed manifest")
    if taxonomy not in contents or _digest_record(contents) != sealed["source_digest"]:
        raise InventoryError("source manifest taxonomy digest does not match its files")
    if (
        _tree_identity(sealed["files"], contents, len(sealed["source_tree"]))
        != sealed["source_tree"]
    ):
        raise InventoryError("source inventory does not reproduce the committed Git tree")
    return sealed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seal").add_argument("--output", type=Path, required=True)
    commands.add_parser("verify").add_argument("--source-manifest", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        sealed = (
            seal_source(arguments.root, arguments.output)
            if arguments.command == "seal"
            else verify_source(arguments.root, arguments.source_manifest)
        )
    except InventoryError as error:
        print(f"test suite source: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps({key: sealed[key] for key in ("source_commit", "source_tree", "source_digest")})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
