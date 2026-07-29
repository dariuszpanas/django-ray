"""Redact configured values before emitting a hard-bounded diagnostic stream."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable, Iterator
from typing import TextIO

_DEFAULT_MAX_CHARS = 65_536
_MAX_SECRET_CHARS = 4_096
_READ_CHUNK_CHARS = 8_192
_REDACTION = "[REDACTED]"


def _normalized_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({secret for secret in secrets if secret}, key=len, reverse=True))


def _redact_complete(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        text = text.replace(secret, _REDACTION)
    return text


def _iter_redacted(
    stream: TextIO,
    *,
    secrets: tuple[str, ...],
    chunk_chars: int,
) -> Iterator[str]:
    """Yield redacted chunks while retaining only a secret-sized raw overlap."""
    if chunk_chars <= 0:
        raise ValueError("chunk_chars must be positive")
    if not secrets:
        while chunk := stream.read(chunk_chars):
            yield chunk
        return

    max_secret_chars = len(secrets[0])
    pending = ""
    while chunk := stream.read(chunk_chars):
        candidate = f"{pending}{chunk}"
        safe_start_limit = max(0, len(candidate) - max_secret_chars + 1)
        index = 0
        output: list[str] = []
        while index < safe_start_limit:
            matched = next(
                (secret for secret in secrets if candidate.startswith(secret, index)),
                None,
            )
            if matched is None:
                output.append(candidate[index])
                index += 1
            else:
                output.append(_REDACTION)
                index += len(matched)
        pending = candidate[index:]
        if output:
            yield "".join(output)
    if pending:
        yield _redact_complete(pending, secrets)


def redact_and_bound(
    text: str,
    *,
    secrets: Iterable[str],
    max_chars: int,
    source_truncated: bool = False,
) -> str:
    """Return redacted diagnostics whose marker-inclusive length is bounded."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    redacted = _redact_complete(text, _normalized_secrets(secrets))

    if not source_truncated and len(redacted) <= max_chars:
        return redacted

    marker = f"[diagnostics truncated; output capped at {max_chars} characters]\n"
    if len(marker) >= max_chars:
        return marker[:max_chars]
    prefix_limit = max_chars - len(marker)
    prefix = redacted[:prefix_limit]
    if prefix and not prefix.endswith("\n"):
        prefix = f"{prefix[:-1]}\n"
    return f"{prefix}{marker}"


def read_redacted_bounded(
    stream: TextIO,
    *,
    secrets: Iterable[str],
    max_chars: int,
    chunk_chars: int = _READ_CHUNK_CHARS,
) -> str:
    """Consume a stream with bounded memory and return redaction-first output."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    normalized_secrets = _normalized_secrets(secrets)
    captured: list[str] = []
    captured_chars = 0
    source_truncated = False

    for chunk in _iter_redacted(
        stream,
        secrets=normalized_secrets,
        chunk_chars=chunk_chars,
    ):
        remaining = max_chars + 1 - captured_chars
        if remaining > 0:
            captured.append(chunk[:remaining])
            captured_chars += min(len(chunk), remaining)
        if len(chunk) > max(remaining, 0):
            source_truncated = True

    if captured_chars > max_chars:
        source_truncated = True
    return redact_and_bound(
        "".join(captured),
        secrets=(),
        max_chars=max_chars,
        source_truncated=source_truncated,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-chars", type=int, default=_DEFAULT_MAX_CHARS)
    parser.add_argument(
        "--secret-env",
        action="append",
        default=[],
        help="Environment variable whose non-empty value must be redacted",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.max_chars <= 0:
        raise ValueError("--max-chars must be positive")

    secrets = [os.environ.get(name, "") for name in args.secret_env]
    if any(len(secret) > _MAX_SECRET_CHARS for secret in secrets):
        raise ValueError("a configured redaction value exceeds the supported length")

    sys.stdout.write(
        read_redacted_bounded(
            sys.stdin,
            secrets=secrets,
            max_chars=args.max_chars,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
