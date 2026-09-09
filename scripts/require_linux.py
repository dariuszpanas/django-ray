"""Reject heavy native test execution outside the supported Linux runtime."""

from __future__ import annotations

import sys


def require_linux(platform: str | None = None) -> None:
    """Fail before starting resource-owning work on a developer host."""
    if (sys.platform if platform is None else platform) != "linux":
        raise SystemExit(
            "Heavy django-ray validation requires Linux. Use an explicitly bounded Linux "
            "environment or the required GitHub Actions Linux CI; no environment is started "
            "automatically. Run only focused resource-free checks locally. Windows best-effort "
            "compatibility checks belong on GitHub Actions Windows runners."
        )


if __name__ == "__main__":
    require_linux()
