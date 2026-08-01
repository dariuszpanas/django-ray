"""Application-owned Ray Data batch-job recipe for the bundled test project.

This module deliberately has no Django or django-ray imports.  The driver calls
``run_ray_data_batch_job`` once from the outer Ray Job process; only the
``DeterministicBatchScorer`` instance is serialized to Ray Data workers.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID

MANIFEST_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
OPERATION_NAME = "deterministic-batch-score-v1"
ARTIFACT_COMPLETE_STATUS = "artifact_complete"
DURABLE_SUCCEEDED_STATE = "SUCCEEDED"

MAX_URI_CHARS = 768
MAX_RUN_KEY_CHARS = 64
MAX_REVISION_CHARS = 128
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_FILES = 64
MAX_OUTPUT_ENTRIES = 128
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_ROWS = 10_000_000
MAX_SCHEMA_FIELDS = 64
MAX_SCHEMA_TEXT_CHARS = 128
MAX_MANIFEST_BYTES = 8 * 1024
MAX_RESULT_BYTES = 4 * 1024
MAX_ATTEMPT_NUMBER = 10_000
MAX_TASK_EXECUTION_PK = (1 << 63) - 1

_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_RUN_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,127}")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_EMPTY_CONTENT_SHA256 = hashlib.sha256().hexdigest()


class RayDataRecipeError(RuntimeError):
    """Base error raised by the application-owned Ray Data recipe."""


class RayDataDependencyError(RayDataRecipeError):
    """The optional Ray Data dependency boundary is unavailable."""


class IncompleteAttemptError(RayDataRecipeError):
    """An attempt directory exists without an authoritative completion manifest."""


class CompletionConflictError(RayDataRecipeError):
    """An existing completion manifest does not identify this request."""


class InputChangedError(RayDataRecipeError):
    """The supposedly immutable input changed while the batch job was running."""


class OutputChangedError(RayDataRecipeError):
    """Completed output no longer matches its authoritative manifest identity."""


class ArtifactNotAdoptableError(RayDataRecipeError):
    """An artifact is not committed by the matching durable task success."""


class DeterministicBatchScorer:
    """Side-effect-free batch transform serialized only to Ray Data workers."""

    def __init__(self, *, scale: float, bias: float) -> None:
        self.scale = scale
        self.bias = bias

    def __call__(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Return the input columns plus one deterministic floating-point score."""
        try:
            values = batch["value"].astype("float64", copy=False)
        except KeyError as error:
            raise ValueError("each input row must contain a numeric 'value' field") from error
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("the input 'value' field must be numeric") from error

        transformed = dict(batch)
        transformed["score"] = values * self.scale + self.bias
        return transformed


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_bounded_text(
    value: str,
    *,
    label: str,
    pattern: re.Pattern[str],
    max_chars: int,
) -> str:
    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise ValueError(f"{label} must contain between 1 and {max_chars} characters")
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{label} contains unsupported characters")
    return value


def _validate_positive_integer(value: int, *, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer between 1 and {maximum}")
    return value


def _validate_number(value: float, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or abs(normalized) > 1_000_000:
        raise ValueError(f"{label} must be finite and between -1000000 and 1000000")
    return normalized


def _validate_task_id(value: str) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("task_id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("task_id must be a canonical UUID") from error
    if str(parsed) != value:
        raise ValueError("task_id must be a lowercase canonical UUID")
    return value


def _path_from_file_uri(uri: str, *, label: str) -> Path:
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_CHARS:
        raise ValueError(f"{label} must be a file URI of at most {MAX_URI_CHARS} characters")
    if any(ord(character) < 32 for character in uri):
        raise ValueError(f"{label} must not contain control characters")

    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "file":
        raise ValueError(f"{label} must use the file URI scheme in this reference recipe")
    if parsed.netloc not in {"", "localhost"}:
        raise ValueError(f"{label} must not contain credentials or a remote authority")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain a query string or fragment")
    if _INVALID_PERCENT_ESCAPE.search(parsed.path) is not None:
        raise ValueError(f"{label} contains invalid percent escaping")

    try:
        decoded_path = unquote(parsed.path, errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} contains invalid UTF-8 escaping") from error
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in decoded_path):
        raise ValueError(f"{label} must not contain encoded control characters")
    if decoded_path.startswith(("//", "\\\\")):
        raise ValueError(f"{label} must not identify a remote or UNC path")
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", decoded_path):
        decoded_path = decoded_path[1:]
    path = Path(decoded_path)
    if not path.is_absolute():
        raise ValueError(f"{label} must identify an absolute path")
    return Path(os.path.abspath(path))


def _file_uri(path: Path) -> str:
    uri = Path(os.path.abspath(path)).as_uri()
    if len(uri) > MAX_URI_CHARS:
        raise ValueError(f"normalized file URI exceeds {MAX_URI_CHARS} characters")
    return uri


def _validate_input_beneath_root(input_path: Path, input_root: Path) -> None:
    """Require a server-controlled, symlink-free parent path for one input."""
    try:
        root_metadata = os.lstat(input_root)
    except OSError as error:
        raise ValueError("configured input root must identify a readable directory") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("configured input root must be a regular, non-symlink directory")

    try:
        relative = input_path.relative_to(input_root)
    except ValueError as error:
        raise ValueError("input_uri must be inside the configured input root") from error
    if not relative.parts:
        raise ValueError("input_uri must identify a file inside the configured input root")

    current = input_root
    for component in relative.parts[:-1]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise ValueError("input_uri parent could not be inspected") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("input_uri must not traverse a linked or non-directory parent")


def _require_disjoint_roots(input_root: Path, output_root: Path) -> None:
    """Keep immutable inputs outside the writable artifact namespace."""
    try:
        input_root.relative_to(output_root)
    except ValueError:
        pass
    else:
        raise ValueError("configured input and output roots must not overlap")
    try:
        output_root.relative_to(input_root)
    except ValueError:
        return
    raise ValueError("configured input and output roots must not overlap")


def _validate_output_root(output_root: Path) -> None:
    """Require an operator-provisioned, non-symlink artifact root."""
    try:
        metadata = os.lstat(output_root)
    except OSError as error:
        raise ValueError("configured output root must identify a readable directory") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("output_root_uri must identify a regular, non-symlink directory")


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _hash_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    expected_bytes: int | None = None,
) -> tuple[int, str]:
    """Return one bounded digest from a stable regular-file snapshot."""
    if maximum_bytes < 0:
        raise RayDataRecipeError("file grew beyond its declared hashing budget")

    digest = hashlib.sha256()
    observed_bytes = 0
    try:
        path_before = os.lstat(path)
        if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(path_before.st_mode):
            raise RayDataRecipeError("file must be a regular, non-symlink file")
        if expected_bytes is not None and path_before.st_size != expected_bytes:
            raise RayDataRecipeError("file size changed while its content was hashed")
        if path_before.st_size > maximum_bytes:
            raise RayDataRecipeError("file grew beyond its declared hashing budget")

        with path.open("rb") as source:
            opened_before = os.fstat(source.fileno())
            if not _same_file_snapshot(path_before, opened_before):
                raise RayDataRecipeError("file changed while its content was hashed")
            while True:
                remaining = maximum_bytes - observed_bytes
                chunk = source.read(min(1024 * 1024, max(0, remaining) + 1))
                if not chunk:
                    break
                observed_bytes += len(chunk)
                if observed_bytes > maximum_bytes:
                    raise RayDataRecipeError("file grew beyond its declared hashing budget")
                digest.update(chunk)
            opened_after = os.fstat(source.fileno())
        path_after = os.lstat(path)
    except OSError as error:
        raise RayDataRecipeError(
            "file could not be inspected while its content was hashed"
        ) from error

    if not (
        _same_file_snapshot(path_before, opened_before)
        and _same_file_snapshot(opened_before, opened_after)
        and _same_file_snapshot(opened_after, path_after)
    ):
        raise RayDataRecipeError("file changed while its content was hashed")
    if observed_bytes != opened_after.st_size:
        raise RayDataRecipeError("file size changed while its content was hashed")
    return observed_bytes, digest.hexdigest()


def _sha256_file(
    path: Path,
    *,
    maximum_bytes: int,
    expected_bytes: int | None = None,
) -> str:
    """Hash at most one declared regular-file budget."""
    return _hash_regular_file(
        path,
        maximum_bytes=maximum_bytes,
        expected_bytes=expected_bytes,
    )[1]


def _validate_input(path: Path, expected_sha256: str) -> int:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise ValueError("input_uri must identify a readable regular file") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("input_uri must identify a regular, non-symlink file")
    size_before = metadata.st_size
    if size_before > MAX_INPUT_BYTES:
        raise ValueError(f"input file exceeds the {MAX_INPUT_BYTES}-byte recipe limit")
    try:
        actual_sha256 = _sha256_file(
            path,
            maximum_bytes=MAX_INPUT_BYTES,
            expected_bytes=size_before,
        )
    except RayDataRecipeError as error:
        raise ValueError("input_uri changed while it was inspected") from error
    if actual_sha256 != expected_sha256:
        raise ValueError("input_sha256 does not match input_uri")
    return size_before


def _load_ray_data() -> tuple[Any, Any, Any]:
    try:
        import ray.data as ray_data
        from ray.data import ActorPoolStrategy

        parquet = importlib.import_module("pyarrow.parquet")
    except (ImportError, ModuleNotFoundError) as error:
        raise RayDataDependencyError(
            "Ray Data is an application dependency; install the matching 'ray[data]' "
            "extra on the Ray Job driver and every cluster worker"
        ) from error
    return ray_data, ActorPoolStrategy, parquet


def _attempt_paths(
    output_root: Path,
    deployment_key: str,
    run_key: str,
    task_id: str,
    task_execution_pk: int,
    execution_generation: int,
    attempt_number: int,
) -> tuple[Path, Path, Path]:
    attempt = (
        output_root
        / "deployments"
        / deployment_key
        / "tasks"
        / task_id
        / "executions"
        / str(task_execution_pk)
        / "runs"
        / run_key
        / f"g-{execution_generation}"
        / f"a-{attempt_number:04d}"
    )
    return attempt, attempt / "data", attempt / "completion.json"


def _validate_existing_attempt_namespace(output_root: Path, attempt_dir: Path) -> None:
    """Reject linked or non-directory components in an existing owned namespace."""
    current = output_root
    for component in attempt_dir.relative_to(output_root).parts:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        except OSError as error:
            raise IncompleteAttemptError("attempt namespace could not be inspected") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise IncompleteAttemptError(
                "attempt namespace contains a linked or non-directory component"
            )


def _prepare_attempt_parent(output_root: Path, attempt_parent: Path) -> None:
    """Create generated namespace directories without following an owned symlink."""
    try:
        root_metadata = os.lstat(output_root)
    except OSError as error:
        raise RayDataRecipeError("configured output root could not be inspected") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise RayDataRecipeError("attempt output root is not a regular directory")

    current = output_root
    for component in attempt_parent.relative_to(output_root).parts:
        current /= component
        try:
            current.mkdir()
        except FileExistsError:
            pass
        except OSError as error:
            raise RayDataRecipeError("attempt namespace could not be prepared") from error
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise RayDataRecipeError("attempt namespace could not be inspected") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RayDataRecipeError(
                "attempt namespace contains a linked or non-directory component"
            )


def _bounded_schema(schema: Any) -> list[dict[str, str]]:
    try:
        field_count = len(schema)
    except Exception as error:
        raise RayDataRecipeError("output schema could not be inspected") from error
    if isinstance(field_count, bool) or not isinstance(field_count, int) or field_count < 0:
        raise RayDataRecipeError("output schema has an invalid field count")
    if field_count > MAX_SCHEMA_FIELDS:
        raise RayDataRecipeError(f"output schema exceeds {MAX_SCHEMA_FIELDS} fields")

    result: list[dict[str, str]] = []
    for index in range(field_count):
        try:
            field = schema[index]
            name = str(field.name)
            type_name = str(field.type)
        except Exception as error:
            raise RayDataRecipeError("output schema could not be inspected") from error
        if not name or len(name) > MAX_SCHEMA_TEXT_CHARS:
            raise RayDataRecipeError("output schema contains an invalid field name")
        if not type_name or len(type_name) > MAX_SCHEMA_TEXT_CHARS:
            raise RayDataRecipeError("output schema contains an invalid field type")
        result.append({"name": name, "type": type_name})
    return result


def _same_directory_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _bounded_relative_path(path: Path, output_dir: Path) -> bytes:
    relative_text = path.relative_to(output_dir).as_posix()
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in relative_text):
        raise RayDataRecipeError("output contains a path with control characters")
    try:
        relative = relative_text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RayDataRecipeError("output contains a path that is not valid UTF-8") from error
    if not relative or len(relative) > MAX_URI_CHARS:
        raise RayDataRecipeError("output contains an oversized relative path")
    return relative


def _list_output_files(output_dir: Path) -> list[Path]:
    """Enumerate one output tree without materializing an unbounded directory."""
    files: list[Path] = []
    pending = [output_dir]
    inspected_directories: list[tuple[Path, os.stat_result]] = []
    entry_count = 0

    while pending:
        directory = pending.pop()
        try:
            directory_before = os.lstat(directory)
            if stat.S_ISLNK(directory_before.st_mode) or not stat.S_ISDIR(directory_before.st_mode):
                raise RayDataRecipeError(
                    "Ray Data output must contain only regular, non-symlink directories"
                )
            inspected_directories.append((directory, directory_before))
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > MAX_OUTPUT_ENTRIES:
                        raise RayDataRecipeError(
                            f"output exceeds the {MAX_OUTPUT_ENTRIES}-entry recipe limit"
                        )
                    path = Path(entry.path)
                    _bounded_relative_path(path, output_dir)
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise RayDataRecipeError("Ray Data output contains a symbolic link")
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(path)
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        raise RayDataRecipeError("Ray Data output contains a non-regular file")
                    if path.suffix != ".parquet":
                        raise RayDataRecipeError(
                            "Ray Data output contains an unexpected non-Parquet file"
                        )
                    files.append(path)
                    if len(files) > MAX_OUTPUT_FILES:
                        raise RayDataRecipeError(
                            f"output exceeds the {MAX_OUTPUT_FILES}-file recipe limit"
                        )
        except RayDataRecipeError:
            raise
        except OSError as error:
            raise RayDataRecipeError("Ray Data output could not be inspected") from error

    try:
        for directory, before in inspected_directories:
            after = os.lstat(directory)
            if not _same_directory_snapshot(before, after):
                raise RayDataRecipeError("Ray Data output changed while it was inspected")
    except OSError as error:
        raise RayDataRecipeError("Ray Data output could not be inspected") from error

    files.sort(key=lambda path: path.relative_to(output_dir).as_posix())
    return files


def _inspect_output_content(output_dir: Path) -> tuple[list[Path], int, str]:
    """Return bounded Parquet files plus one path-and-content identity."""
    files = _list_output_files(output_dir)
    content_digest = hashlib.sha256()
    total_bytes = 0
    for path in files:
        relative = _bounded_relative_path(path, output_dir)
        try:
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise RayDataRecipeError("Ray Data output contains a non-regular file")
            size_before = metadata.st_size
            if size_before < 0 or total_bytes + size_before > MAX_OUTPUT_BYTES:
                raise RayDataRecipeError(f"output exceeds the {MAX_OUTPUT_BYTES}-byte recipe limit")
            file_digest = _sha256_file(
                path,
                maximum_bytes=size_before,
                expected_bytes=size_before,
            )
        except RayDataRecipeError:
            raise
        except OSError as error:
            raise RayDataRecipeError("Ray Data output could not be inspected") from error
        total_bytes += size_before
        content_digest.update(len(relative).to_bytes(4, "big"))
        content_digest.update(relative)
        content_digest.update(size_before.to_bytes(8, "big"))
        content_digest.update(bytes.fromhex(file_digest))

    final_files = _list_output_files(output_dir)
    if [path.relative_to(output_dir) for path in final_files] != [
        path.relative_to(output_dir) for path in files
    ]:
        raise RayDataRecipeError("Ray Data output changed while it was inspected")
    return files, total_bytes, content_digest.hexdigest()


def _ensure_output_directory_after_write(output_dir: Path) -> None:
    """Materialize Ray Data's successful empty-write outcome explicitly."""
    try:
        metadata = os.lstat(output_dir)
    except FileNotFoundError:
        try:
            output_dir.mkdir()
        except FileExistsError:
            try:
                metadata = os.lstat(output_dir)
            except OSError as error:
                raise RayDataRecipeError("Ray Data output could not be inspected") from error
        except OSError as error:
            raise RayDataRecipeError("empty Ray Data output could not be materialized") from error
        else:
            try:
                metadata = os.lstat(output_dir)
            except OSError as error:
                raise RayDataRecipeError("Ray Data output could not be inspected") from error
    except OSError as error:
        raise RayDataRecipeError("Ray Data output could not be inspected") from error

    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RayDataRecipeError("Ray Data output must be a regular, non-symlink directory")


def _inspect_parquet_output(
    output_dir: Path, parquet: Any
) -> tuple[int, int, list[dict[str, str]], int, str]:
    files, total_bytes, content_sha256 = _inspect_output_content(output_dir)

    row_count = 0
    output_schema: Any | None = None
    try:
        for path in files:
            path_before = os.lstat(path)
            with path.open("rb") as source:
                opened_before = os.fstat(source.fileno())
                if not _same_file_snapshot(path_before, opened_before):
                    raise RayDataRecipeError("Ray Data output changed before metadata inspection")
                parquet_file = parquet.ParquetFile(source)
                file_rows = parquet_file.metadata.num_rows
                if isinstance(file_rows, bool) or not isinstance(file_rows, int) or file_rows < 0:
                    raise RayDataRecipeError("output Parquet metadata has an invalid row count")
                row_count += file_rows
                if row_count > MAX_OUTPUT_ROWS:
                    raise RayDataRecipeError(
                        f"output exceeds the {MAX_OUTPUT_ROWS}-row recipe limit"
                    )
                if output_schema is None:
                    output_schema = parquet_file.schema_arrow
                elif not output_schema.equals(parquet_file.schema_arrow):
                    raise RayDataRecipeError("output Parquet files do not share one schema")
                opened_after = os.fstat(source.fileno())
            path_after = os.lstat(path)
            if not (
                _same_file_snapshot(path_before, opened_before)
                and _same_file_snapshot(opened_before, opened_after)
                and _same_file_snapshot(opened_after, path_after)
            ):
                raise RayDataRecipeError("Ray Data output changed during metadata inspection")
    except RayDataRecipeError:
        raise
    except Exception as error:
        raise RayDataRecipeError(
            "Ray Data output Parquet metadata could not be inspected"
        ) from error

    schema = [] if output_schema is None else _bounded_schema(output_schema)
    content_files, verified_bytes, verified_sha256 = _inspect_output_content(output_dir)
    if [path.relative_to(output_dir) for path in content_files] != [
        path.relative_to(output_dir) for path in files
    ] or (verified_bytes, verified_sha256) != (total_bytes, content_sha256):
        raise RayDataRecipeError("Ray Data output changed while it was inspected")
    return row_count, len(files), schema, total_bytes, content_sha256


def _verify_output_content(
    output_dir: Path,
    *,
    expected_file_count: int,
    expected_total_bytes: int,
    expected_content_sha256: str,
) -> None:
    try:
        files, total_bytes, content_sha256 = _inspect_output_content(output_dir)
    except RayDataRecipeError:
        raise OutputChangedError(
            "completed Ray Data output no longer matches its manifest"
        ) from None
    if (
        len(files) != expected_file_count
        or total_bytes != expected_total_bytes
        or content_sha256 != expected_content_sha256
    ):
        raise OutputChangedError("completed Ray Data output no longer matches its manifest")


def _build_manifest(
    *,
    deployment_key: str,
    run_key: str,
    task_id: str,
    task_execution_pk: int,
    execution_generation: int,
    attempt_number: int,
    input_uri: str,
    input_sha256: str,
    output_uri: str,
    row_count: int,
    file_count: int,
    total_bytes: int,
    content_sha256: str,
    output_schema: list[dict[str, str]],
    application_revision: str,
    model_revision: str,
    scale: float,
    bias: float,
) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": ARTIFACT_COMPLETE_STATUS,
        "run": {
            "deployment_key": deployment_key,
            "key": run_key,
            "task_id": task_id,
            "task_execution_pk": task_execution_pk,
            "execution_generation": execution_generation,
            "attempt_number": attempt_number,
        },
        "input": {"uri": input_uri, "sha256": input_sha256},
        "operation": {"name": OPERATION_NAME, "scale": scale, "bias": bias},
        "application": {
            "revision": application_revision,
            "model_revision": model_revision,
        },
        "output": {
            "uri": output_uri,
            "format": "parquet",
            "row_count": row_count,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "content_sha256": content_sha256,
            "schema": output_schema,
        },
        "summary": {"outcome": ARTIFACT_COMPLETE_STATUS},
    }


def _publish_completion_manifest(path: Path, manifest: dict[str, Any]) -> bytes:
    """Publish once with process-atomic visibility, not power-loss durability.

    The temporary file is flushed and fsynced before the create-only hard link. The
    recipe deliberately leaves filesystem/output durability and parent-directory fsync
    requirements to the deployment's tested storage contract.
    """
    encoded = _canonical_json(manifest)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise RayDataRecipeError(f"completion manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise RayDataRecipeError(
            "completion manifest destination could not be inspected"
        ) from error
    else:
        raise CompletionConflictError("completion manifest already exists")

    temporary: Path | None = None
    try:
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=".completion.json.",
                suffix=".tmp",
                delete=False,
            ) as target:
                temporary = Path(target.name)
                if os.name != "nt":
                    os.fchmod(target.fileno(), 0o640)
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
        except OSError as error:
            raise RayDataRecipeError("completion manifest could not be written") from error

        try:
            assert temporary is not None
            os.link(temporary, path)
        except FileExistsError as error:
            raise CompletionConflictError("completion manifest already exists") from error
        except OSError as error:
            raise RayDataRecipeError(
                "completion manifest requires process-atomic same-directory hard-link publication"
            ) from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A published hard link remains authoritative. A leftover temporary
                # file is bounded diagnostic residue, not permission to replace it.
                pass
    return encoded


def _read_completion_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        path_before = os.lstat(path)
        if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(path_before.st_mode):
            raise CompletionConflictError("completion manifest must be a regular, non-symlink file")
        with path.open("rb") as source:
            opened_before = os.fstat(source.fileno())
            if not stat.S_ISREG(opened_before.st_mode):
                raise CompletionConflictError(
                    "completion manifest must be a regular, non-symlink file"
                )
            if not 1 <= opened_before.st_size <= MAX_MANIFEST_BYTES:
                raise CompletionConflictError("completion manifest has an invalid size")
            encoded = source.read(MAX_MANIFEST_BYTES + 1)
            opened_after = os.fstat(source.fileno())
        path_after = os.lstat(path)
    except OSError as error:
        raise CompletionConflictError("completion manifest could not be read") from error
    if not 1 <= len(encoded) <= MAX_MANIFEST_BYTES:
        raise CompletionConflictError("completion manifest has an invalid size")
    opened_identity = (opened_before.st_dev, opened_before.st_ino)
    if (
        stat.S_ISLNK(path_after.st_mode)
        or not stat.S_ISREG(path_after.st_mode)
        or (path_before.st_dev, path_before.st_ino) != opened_identity
        or (path_after.st_dev, path_after.st_ino) != opened_identity
        or not _same_file_snapshot(path_before, opened_before)
        or not _same_file_snapshot(opened_before, opened_after)
        or not _same_file_snapshot(opened_after, path_after)
        or opened_before.st_size != opened_after.st_size
        or opened_before.st_mtime_ns != opened_after.st_mtime_ns
        or len(encoded) != opened_after.st_size
    ):
        raise CompletionConflictError("completion manifest changed while it was read")
    try:
        manifest = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise CompletionConflictError("completion manifest is not valid JSON") from error
    try:
        is_canonical = isinstance(manifest, dict) and _canonical_json(manifest) == encoded
    except (TypeError, ValueError, RecursionError) as error:
        raise CompletionConflictError("completion manifest is not canonical JSON") from error
    if not is_canonical:
        raise CompletionConflictError("completion manifest is not canonical JSON")
    return manifest, encoded


def _validate_existing_manifest(
    manifest: dict[str, Any],
    *,
    deployment_key: str,
    run_key: str,
    task_id: str,
    task_execution_pk: int,
    execution_generation: int,
    attempt_number: int,
    input_uri: str,
    input_sha256: str,
    output_uri: str,
    application_revision: str,
    model_revision: str,
    scale: float,
    bias: float,
) -> None:
    expected_identity = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": ARTIFACT_COMPLETE_STATUS,
        "run": {
            "deployment_key": deployment_key,
            "key": run_key,
            "task_id": task_id,
            "task_execution_pk": task_execution_pk,
            "execution_generation": execution_generation,
            "attempt_number": attempt_number,
        },
        "input": {"uri": input_uri, "sha256": input_sha256},
        "operation": {"name": OPERATION_NAME, "scale": scale, "bias": bias},
        "application": {
            "revision": application_revision,
            "model_revision": model_revision,
        },
        "summary": {"outcome": ARTIFACT_COMPLETE_STATUS},
    }
    for field, expected in expected_identity.items():
        if manifest.get(field) != expected:
            raise CompletionConflictError(f"completion manifest has a conflicting {field}")

    if set(manifest) != {*expected_identity, "output"}:
        raise CompletionConflictError("completion manifest has unsupported fields")
    output = manifest.get("output")
    if not isinstance(output, dict) or set(output) != {
        "uri",
        "format",
        "row_count",
        "file_count",
        "total_bytes",
        "content_sha256",
        "schema",
    }:
        raise CompletionConflictError("completion manifest has an invalid output summary")
    if output.get("uri") != output_uri or output.get("format") != "parquet":
        raise CompletionConflictError("completion manifest identifies a different output")

    row_count = output.get("row_count")
    file_count = output.get("file_count")
    total_bytes = output.get("total_bytes")
    content_sha256 = output.get("content_sha256")
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or not 0 <= row_count <= MAX_OUTPUT_ROWS
        or isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or not 0 <= file_count <= MAX_OUTPUT_FILES
        or isinstance(total_bytes, bool)
        or not isinstance(total_bytes, int)
        or not 0 <= total_bytes <= MAX_OUTPUT_BYTES
        or not isinstance(content_sha256, str)
        or _HEX_DIGEST.fullmatch(content_sha256) is None
    ):
        raise CompletionConflictError("completion manifest has invalid output counts")
    schema = output.get("schema")
    if not isinstance(schema, list) or len(schema) > MAX_SCHEMA_FIELDS:
        raise CompletionConflictError("completion manifest has an invalid output schema")
    for field in schema:
        if not isinstance(field, dict) or set(field) != {"name", "type"}:
            raise CompletionConflictError("completion manifest has an invalid output schema")
        if not all(
            isinstance(field[key], str) and 1 <= len(field[key]) <= MAX_SCHEMA_TEXT_CHARS
            for key in ("name", "type")
        ):
            raise CompletionConflictError("completion manifest has an invalid output schema")
    if file_count == 0:
        if row_count != 0 or total_bytes != 0 or schema or content_sha256 != _EMPTY_CONTENT_SHA256:
            raise CompletionConflictError("completion manifest has an inconsistent empty output")
    elif total_bytes == 0 or (row_count > 0 and not schema):
        raise CompletionConflictError("completion manifest has inconsistent Parquet output")


def _completion_result(
    manifest: dict[str, Any], *, manifest_uri: str, manifest_bytes: bytes
) -> dict[str, Any]:
    output = manifest["output"]
    application = manifest["application"]
    run = manifest["run"]
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": ARTIFACT_COMPLETE_STATUS,
        "deployment_key": run["deployment_key"],
        "run_key": run["key"],
        "task_id": run["task_id"],
        "task_execution_pk": run["task_execution_pk"],
        "execution_generation": run["execution_generation"],
        "attempt_number": run["attempt_number"],
        "manifest_uri": manifest_uri,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "output_uri": output["uri"],
        "row_count": output["row_count"],
        "file_count": output["file_count"],
        "output_bytes": output["total_bytes"],
        "output_sha256": output["content_sha256"],
        "application_revision": application["revision"],
        "model_revision": application["model_revision"],
    }
    if len(_canonical_json(result)) > MAX_RESULT_BYTES:
        raise RayDataRecipeError(f"Django task result exceeds {MAX_RESULT_BYTES} bytes")
    return result


def validate_adoptable_artifact(
    result: dict[str, Any],
    *,
    durable_state: str,
    output_root_uri: str,
    deployment_key: str,
    task_id: str,
    task_execution_pk: int,
    execution_generation: int,
    attempt_number: int,
) -> dict[str, Any]:
    """Validate one artifact only after the matching task durably succeeded.

    This application helper deliberately keeps Django out of the artifact module. The
    authorized caller must read the canonical execution row, pass its durable state and
    identity here, and only then persist an application reference to the returned
    manifest.
    """
    if durable_state != DURABLE_SUCCEEDED_STATE:
        raise ArtifactNotAdoptableError(
            "Ray Data artifacts require the matching durable task state SUCCEEDED"
        )
    if not isinstance(result, dict):
        raise ArtifactNotAdoptableError("Ray Data task result must be a JSON object")
    if set(result) != {
        "schema_version",
        "status",
        "deployment_key",
        "run_key",
        "task_id",
        "task_execution_pk",
        "execution_generation",
        "attempt_number",
        "manifest_uri",
        "manifest_sha256",
        "output_uri",
        "row_count",
        "file_count",
        "output_bytes",
        "output_sha256",
        "application_revision",
        "model_revision",
    }:
        raise ArtifactNotAdoptableError("Ray Data task result has unsupported fields")

    deployment_key = _validate_bounded_text(
        deployment_key,
        label="deployment_key",
        pattern=_RUN_KEY,
        max_chars=MAX_RUN_KEY_CHARS,
    )
    task_id = _validate_task_id(task_id)
    task_execution_pk = _validate_positive_integer(
        task_execution_pk,
        label="task_execution_pk",
        maximum=MAX_TASK_EXECUTION_PK,
    )
    execution_generation = _validate_positive_integer(
        execution_generation,
        label="execution_generation",
        maximum=MAX_TASK_EXECUTION_PK,
    )
    attempt_number = _validate_positive_integer(
        attempt_number,
        label="attempt_number",
        maximum=MAX_ATTEMPT_NUMBER,
    )
    run_key = result.get("run_key")
    if not isinstance(run_key, str):
        raise ArtifactNotAdoptableError("Ray Data task result has an invalid run key")
    run_key = _validate_bounded_text(
        run_key,
        label="run_key",
        pattern=_RUN_KEY,
        max_chars=MAX_RUN_KEY_CHARS,
    )

    output_root = _path_from_file_uri(output_root_uri, label="output_root_uri")
    _, expected_output, expected_manifest = _attempt_paths(
        output_root,
        deployment_key,
        run_key,
        task_id,
        task_execution_pk,
        execution_generation,
        attempt_number,
    )
    _validate_output_root(output_root)
    _validate_existing_attempt_namespace(output_root, expected_output.parent)
    expected_identity = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": ARTIFACT_COMPLETE_STATUS,
        "deployment_key": deployment_key,
        "task_id": task_id,
        "task_execution_pk": task_execution_pk,
        "execution_generation": execution_generation,
        "attempt_number": attempt_number,
        "manifest_uri": _file_uri(expected_manifest),
        "output_uri": _file_uri(expected_output),
    }
    for field, expected in expected_identity.items():
        if result.get(field) != expected:
            raise ArtifactNotAdoptableError(
                f"Ray Data task result does not match durable identity field {field}"
            )

    manifest_sha256 = result.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or _HEX_DIGEST.fullmatch(manifest_sha256) is None:
        raise ArtifactNotAdoptableError("Ray Data task result has an invalid manifest digest")
    manifest, encoded = _read_completion_manifest(expected_manifest)
    if hashlib.sha256(encoded).hexdigest() != manifest_sha256:
        raise ArtifactNotAdoptableError("Ray Data completion manifest digest changed")

    input_summary = manifest.get("input")
    operation = manifest.get("operation")
    application = manifest.get("application")
    if (
        not isinstance(input_summary, dict)
        or set(input_summary) != {"uri", "sha256"}
        or not isinstance(operation, dict)
        or set(operation) != {"name", "scale", "bias"}
        or not isinstance(application, dict)
        or set(application) != {"revision", "model_revision"}
    ):
        raise ArtifactNotAdoptableError("Ray Data manifest has an invalid request identity")
    input_uri = input_summary.get("uri")
    input_sha256 = input_summary.get("sha256")
    application_revision = application.get("revision")
    model_revision = application.get("model_revision")
    try:
        if not isinstance(input_uri, str):
            raise ValueError("input URI is not text")
        _path_from_file_uri(input_uri, label="manifest input URI")
        if not isinstance(input_sha256, str) or _HEX_DIGEST.fullmatch(input_sha256) is None:
            raise ValueError("input digest is invalid")
        application_revision = _validate_bounded_text(
            application_revision,
            label="application_revision",
            pattern=_REVISION,
            max_chars=MAX_REVISION_CHARS,
        )
        model_revision = _validate_bounded_text(
            model_revision,
            label="model_revision",
            pattern=_REVISION,
            max_chars=MAX_REVISION_CHARS,
        )
        scale = _validate_number(operation.get("scale"), label="scale")
        bias = _validate_number(operation.get("bias"), label="bias")
        _validate_existing_manifest(
            manifest,
            deployment_key=deployment_key,
            run_key=run_key,
            task_id=task_id,
            task_execution_pk=task_execution_pk,
            execution_generation=execution_generation,
            attempt_number=attempt_number,
            input_uri=input_uri,
            input_sha256=input_sha256,
            output_uri=_file_uri(expected_output),
            application_revision=application_revision,
            model_revision=model_revision,
            scale=scale,
            bias=bias,
        )
    except (ValueError, CompletionConflictError) as error:
        raise ArtifactNotAdoptableError("Ray Data manifest is not a valid artifact") from error

    if (
        result.get("application_revision") != application_revision
        or result.get("model_revision") != model_revision
    ):
        raise ArtifactNotAdoptableError("Ray Data result and manifest revisions differ")

    run = manifest.get("run")
    if not isinstance(run, dict) or any(
        run.get(field) != expected
        for field, expected in {
            "deployment_key": deployment_key,
            "key": run_key,
            "task_id": task_id,
            "task_execution_pk": task_execution_pk,
            "execution_generation": execution_generation,
            "attempt_number": attempt_number,
        }.items()
    ):
        raise ArtifactNotAdoptableError("Ray Data manifest identifies another durable execution")
    if manifest.get("status") != ARTIFACT_COMPLETE_STATUS:
        raise ArtifactNotAdoptableError("Ray Data manifest is not artifact-complete")

    output = manifest.get("output")
    if not isinstance(output, dict) or any(
        result.get(result_field) != output.get(manifest_field)
        for result_field, manifest_field in {
            "output_uri": "uri",
            "row_count": "row_count",
            "file_count": "file_count",
            "output_bytes": "total_bytes",
            "output_sha256": "content_sha256",
        }.items()
    ):
        raise ArtifactNotAdoptableError("Ray Data result and manifest output identities differ")

    file_count = output.get("file_count")
    total_bytes = output.get("total_bytes")
    content_sha256 = output.get("content_sha256")
    if (
        isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or not 0 <= file_count <= MAX_OUTPUT_FILES
        or isinstance(total_bytes, bool)
        or not isinstance(total_bytes, int)
        or not 0 <= total_bytes <= MAX_OUTPUT_BYTES
        or not isinstance(content_sha256, str)
        or _HEX_DIGEST.fullmatch(content_sha256) is None
    ):
        raise ArtifactNotAdoptableError("Ray Data manifest has invalid output identity")
    _verify_output_content(
        expected_output,
        expected_file_count=file_count,
        expected_total_bytes=total_bytes,
        expected_content_sha256=content_sha256,
    )
    return manifest


def _completed_attempt(
    attempt_dir: Path,
    output_dir: Path,
    completion_path: Path,
    *,
    manifest_uri: str,
    validation: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        attempt_metadata = os.lstat(attempt_dir)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise IncompleteAttemptError("attempt path could not be inspected") from error
    if stat.S_ISLNK(attempt_metadata.st_mode) or not stat.S_ISDIR(attempt_metadata.st_mode):
        raise IncompleteAttemptError("attempt path is not a regular directory")
    try:
        os.lstat(completion_path)
    except FileNotFoundError:
        raise IncompleteAttemptError(
            "attempt output exists without completion.json; retain it for diagnosis or "
            "remove it only after proving no writer is active, then use a new attempt"
        ) from None
    except OSError as error:
        raise CompletionConflictError("completion manifest could not be inspected") from error

    manifest, encoded = _read_completion_manifest(completion_path)
    _validate_existing_manifest(manifest, **validation)
    output = manifest["output"]
    _verify_output_content(
        output_dir,
        expected_file_count=output["file_count"],
        expected_total_bytes=output["total_bytes"],
        expected_content_sha256=output["content_sha256"],
    )
    return _completion_result(
        manifest,
        manifest_uri=manifest_uri,
        manifest_bytes=encoded,
    )


def run_ray_data_batch_job(
    *,
    input_uri: str,
    input_sha256: str,
    input_root_uri: str,
    output_root_uri: str,
    deployment_key: str,
    run_key: str,
    task_id: str,
    application_revision: str,
    model_revision: str,
    task_execution_pk: int,
    execution_generation: int,
    attempt_number: int,
    scale: float = 2.0,
    bias: float = 1.0,
) -> dict[str, Any]:
    """Execute one finite Ray Data transform and publish an attempt manifest.

    Only bounded JSON metadata is returned.  Dataset, ObjectRef, batch, row, and
    framework-handle objects remain inside the outer Ray Job process.
    """
    deployment_key = _validate_bounded_text(
        deployment_key,
        label="deployment_key",
        pattern=_RUN_KEY,
        max_chars=MAX_RUN_KEY_CHARS,
    )
    run_key = _validate_bounded_text(
        run_key,
        label="run_key",
        pattern=_RUN_KEY,
        max_chars=MAX_RUN_KEY_CHARS,
    )
    application_revision = _validate_bounded_text(
        application_revision,
        label="application_revision",
        pattern=_REVISION,
        max_chars=MAX_REVISION_CHARS,
    )
    model_revision = _validate_bounded_text(
        model_revision,
        label="model_revision",
        pattern=_REVISION,
        max_chars=MAX_REVISION_CHARS,
    )
    task_id = _validate_task_id(task_id)
    if not isinstance(input_sha256, str) or _HEX_DIGEST.fullmatch(input_sha256) is None:
        raise ValueError("input_sha256 must be a lowercase SHA-256 digest")
    task_execution_pk = _validate_positive_integer(
        task_execution_pk,
        label="task_execution_pk",
        maximum=MAX_TASK_EXECUTION_PK,
    )
    execution_generation = _validate_positive_integer(
        execution_generation,
        label="execution_generation",
        maximum=MAX_TASK_EXECUTION_PK,
    )
    attempt_number = _validate_positive_integer(
        attempt_number,
        label="attempt_number",
        maximum=MAX_ATTEMPT_NUMBER,
    )
    scale = _validate_number(scale, label="scale")
    bias = _validate_number(bias, label="bias")

    input_path = _path_from_file_uri(input_uri, label="input_uri")
    input_root = _path_from_file_uri(input_root_uri, label="input_root_uri")
    output_root = _path_from_file_uri(output_root_uri, label="output_root_uri")
    normalized_input_uri = _file_uri(input_path)
    _file_uri(input_root)
    _file_uri(output_root)
    _validate_input_beneath_root(input_path, input_root)
    _require_disjoint_roots(input_root, output_root)
    _validate_output_root(output_root)

    attempt_dir, output_dir, completion_path = _attempt_paths(
        output_root,
        deployment_key,
        run_key,
        task_id,
        task_execution_pk,
        execution_generation,
        attempt_number,
    )
    output_uri = _file_uri(output_dir)
    manifest_uri = _file_uri(completion_path)
    validation = {
        "deployment_key": deployment_key,
        "run_key": run_key,
        "task_id": task_id,
        "task_execution_pk": task_execution_pk,
        "execution_generation": execution_generation,
        "attempt_number": attempt_number,
        "input_uri": normalized_input_uri,
        "input_sha256": input_sha256,
        "output_uri": output_uri,
        "application_revision": application_revision,
        "model_revision": model_revision,
        "scale": scale,
        "bias": bias,
    }

    _validate_existing_attempt_namespace(output_root, attempt_dir)
    completed = _completed_attempt(
        attempt_dir,
        output_dir,
        completion_path,
        manifest_uri=manifest_uri,
        validation=validation,
    )
    if completed is not None:
        return completed

    input_bytes = _validate_input(input_path, input_sha256)
    ray_data, actor_pool_strategy, parquet = _load_ray_data()
    _prepare_attempt_parent(output_root, attempt_dir.parent)
    try:
        attempt_dir.mkdir()
    except FileExistsError:
        completed = _completed_attempt(
            attempt_dir,
            output_dir,
            completion_path,
            manifest_uri=manifest_uri,
            validation=validation,
        )
        if completed is not None:
            return completed
        raise AssertionError("unreachable incomplete-attempt state") from None
    except OSError as error:
        raise RayDataRecipeError("attempt namespace could not be reserved") from error
    _validate_existing_attempt_namespace(output_root, attempt_dir)

    dataset = ray_data.read_json(str(input_path))
    transformed = dataset.map_batches(
        DeterministicBatchScorer,
        fn_constructor_kwargs={"scale": scale, "bias": bias},
        compute=actor_pool_strategy(size=1),
        batch_size=256,
        batch_format="numpy",
        zero_copy_batch=True,
        udf_modifying_row_count=False,
        num_cpus=1,
    )
    transformed.write_parquet(str(output_dir), mode="error")
    _ensure_output_directory_after_write(output_dir)

    row_count, file_count, output_schema, total_bytes, content_sha256 = _inspect_parquet_output(
        output_dir, parquet
    )
    try:
        final_input_sha256 = _sha256_file(
            input_path,
            maximum_bytes=MAX_INPUT_BYTES,
            expected_bytes=input_bytes,
        )
        final_input_metadata = os.lstat(input_path)
    except (OSError, RayDataRecipeError):
        raise InputChangedError("input_uri changed before completion could be published") from None
    if (
        stat.S_ISLNK(final_input_metadata.st_mode)
        or not stat.S_ISREG(final_input_metadata.st_mode)
        or final_input_metadata.st_size != input_bytes
        or final_input_sha256 != input_sha256
    ):
        raise InputChangedError("input_uri changed before completion could be published")

    manifest = _build_manifest(
        deployment_key=deployment_key,
        run_key=run_key,
        task_id=task_id,
        task_execution_pk=task_execution_pk,
        execution_generation=execution_generation,
        attempt_number=attempt_number,
        input_uri=normalized_input_uri,
        input_sha256=input_sha256,
        output_uri=output_uri,
        row_count=row_count,
        file_count=file_count,
        total_bytes=total_bytes,
        content_sha256=content_sha256,
        output_schema=output_schema,
        application_revision=application_revision,
        model_revision=model_revision,
        scale=scale,
        bias=bias,
    )
    manifest_bytes = _publish_completion_manifest(completion_path, manifest)
    return _completion_result(
        manifest,
        manifest_uri=manifest_uri,
        manifest_bytes=manifest_bytes,
    )
