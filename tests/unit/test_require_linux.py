from __future__ import annotations

import pytest

from scripts.require_linux import require_linux


@pytest.mark.parametrize("platform", ["win32", "darwin", "freebsd14", "unknown"])
def test_heavy_validation_rejects_non_linux_with_actionable_guidance(platform: str) -> None:
    with pytest.raises(SystemExit, match="requires Linux") as error:
        require_linux(platform)

    message = str(error.value)
    assert "no environment is started automatically" in message
    assert "focused resource-free checks" in message
    assert "GitHub Actions Windows runners" in message


def test_heavy_validation_accepts_linux() -> None:
    require_linux("linux")
