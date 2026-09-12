"""Resolve the exact interpreter from the reviewed stock image during hosted build.

This fixed no-network process runs only inside the admitted hosted build boundary,
before Kubernetes exists. It neither launches Ray nor installs anything.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

PROBE = (
    "import json,platform,sys;print(json.dumps({"
    "'implementation':platform.python_implementation(),"
    "'version':list(sys.version_info[:3])}))"
)


def stock_image(profile: Path) -> str:
    text = profile.read_text(encoding="utf-8")
    images = re.findall(r"^\s+image: (rayproject/ray@sha256:[0-9a-f]{64})$", text, re.MULTILINE)
    if len(images) != 4 or len(set(images)) != 1:
        raise ValueError("The two stock generations must use one reviewed digest")
    return images[0]


def image_python(image: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9./:_-]*@sha256:[0-9a-f]{64}", image):
        raise ValueError("Interpreter discovery requires a pinned image")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cpus=0.25",
            "--memory=128m",
            "--pids-limit=32",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--entrypoint=python",
            image,
            "-c",
            PROBE,
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )
    if result.returncode or not 0 < len(result.stdout) <= 4096:
        raise ValueError("Image interpreter discovery failed")
    value = json.loads(result.stdout)
    version = value.get("version") if type(value) is dict else None
    if (
        type(value) is not dict
        or set(value) != {"implementation", "version"}
        or value["implementation"] != "CPython"
        or type(version) is not list
        or len(version) != 3
        or any(type(item) is not int or not 0 <= item <= 99 for item in version)
        or version[:2] != [3, 12]
    ):
        raise ValueError("Stock image lacks the qualified CPython 3.12 tuple")
    return ".".join(map(str, version))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--profile", type=Path)
    source.add_argument("--image")
    parser.add_argument("--expected")
    args = parser.parse_args(argv)
    try:
        patch = image_python(stock_image(args.profile) if args.profile else args.image)
        if args.expected is not None and patch != args.expected:
            raise ValueError("Application interpreter differs from the stock image")
    except Exception:
        print("Image interpreter qualification failed", flush=True)
        return 1
    print(patch, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
