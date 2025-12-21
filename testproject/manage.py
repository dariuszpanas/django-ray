#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys
from pathlib import Path


def main() -> None:
    """Run administrative tasks."""
    # Get the project root (parent of testproject)
    project_root = Path(__file__).resolve().parent.parent

    # Add src to path so django_ray is importable during development
    src_path = str(project_root / "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    # Add project root to path so testproject module is importable
    root_path = str(project_root)
    if root_path not in sys.path:
        sys.path.insert(0, root_path)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testproject.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
