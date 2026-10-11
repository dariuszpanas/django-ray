"""Verify that an exact released input deleter rejects current rq2 references.

Run this resource-free probe in an isolated environment containing the requested
released distribution, without a source-path override. No Ray or cloud client is
started. Only the owned temporary filesystem fixture is written.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def verify_released_rejection(version: str) -> dict[str, object]:
    """Exercise the installed public deletion entry point with an I/O tripwire."""
    if version not in {"0.4.0", "0.5.0", "0.6.0"}:
        raise ValueError("Unsupported released reference-parser baseline")
    distribution = importlib.metadata.distribution("django-ray")
    if distribution.version != version:
        raise ValueError("Installed reference-parser baseline differs")

    import django_ray.input_storage as input_storage
    from django_ray.result_storage import FilesystemResultStorage

    installed = Path(distribution.locate_file("django_ray/input_storage.py")).resolve()
    if Path(input_storage.__file__).resolve() != installed:
        raise ValueError("Reference-parser probe did not import the installed artifact")
    payload = '{"reference_compatibility":true}'
    digest = hashlib.sha256(payload.encode()).hexdigest()
    size = len(payload.encode())
    key = f"rq2-fixture/{digest[:2]}/{digest[2:4]}/{digest}.json"
    refused = 0
    with TemporaryDirectory(prefix="django-ray-reference-compatibility-") as directory:
        filesystem = FilesystemResultStorage(directory)
        native = filesystem.store_payload(serialized_payload=payload)
        cases = (
            (
                native,
                {"INPUT_STORAGE_BACKEND": "filesystem", "INPUT_STORAGE_FILESYSTEM_PATH": directory},
            ),
            (
                f"s3://owned-fixture/{key}?bytes={size}",
                {
                    "INPUT_STORAGE_BACKEND": "s3",
                    "INPUT_STORAGE_S3_BUCKET": "owned-fixture",
                    "INPUT_STORAGE_S3_PREFIX": "rq2-fixture",
                },
            ),
            (
                f"gs://owned-fixture/{key}?bytes={size}",
                {
                    "INPUT_STORAGE_BACKEND": "gcs",
                    "INPUT_STORAGE_GCS_BUCKET": "owned-fixture",
                    "INPUT_STORAGE_GCS_PREFIX": "rq2-fixture",
                },
            ),
        )
        for reference, config in cases:
            with patch.object(
                input_storage,
                "_storage_backend_for_reference",
                side_effect=AssertionError(
                    "Released deleter selected a backend for a wrapped reference"
                ),
            ) as backend:
                try:
                    input_storage.delete_input_reference("rq2ref:v1:" + reference, config=config)
                except input_storage.InputPayloadValidationError:
                    refused += 1
                else:
                    raise AssertionError("Released deleter accepted a wrapped reference")
                if backend.called:
                    raise AssertionError("Released deleter selected a storage backend")
        if filesystem.load(reference=native) != payload:
            raise AssertionError("Released parser rejection did not preserve fixture bytes")
    if Path(directory).exists():
        raise AssertionError("Reference-parser fixture cleanup was not observed")
    return {
        "schema_version": 1,
        "baseline": version,
        "rejections": refused,
        "fixture_preserved": True,
        "cleanup": "passed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, choices=("0.4.0", "0.5.0", "0.6.0"))
    args = parser.parse_args(argv)
    try:
        receipt = verify_released_rejection(args.version)
    except Exception:
        # Public diagnostics never include installed paths or dependency errors.
        print(json.dumps({"schema_version": 1, "baseline": args.version, "status": "failed"}))
        return 1
    print(json.dumps({**receipt, "status": "passed"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
