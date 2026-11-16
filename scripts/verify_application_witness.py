"""Run from an operator-reviewed immutable copy with python -I, never a candidate."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def unavailable(code):
    print(
        json.dumps(
            {
                "status": "unavailable",
                "metadata_binding_verified": False,
                "semantic_validation": "not_performed",
                "qualification_admission": False,
                "reuse_enabled": False,
                "complete_application_gate": False,
                "current_head_execution": False,
                "self_authenticating": False,
                "authorizes_workflow_review_or_merge": False,
                "failure_codes": [code],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 2


def main():
    sys.dont_write_bytecode = True
    # Isolated mode excludes the caller's cwd/PYTHONPATH from module resolution.
    # Only this trusted copy's private package is loaded; --git-repository is data.
    if not sys.flags.isolated:
        return unavailable("trusted_isolation_required")
    try:
        root = Path(__file__).resolve().parents[1] / "qualification/witness_binding"
        name = "_django_ray_trusted_witness_binding"
        spec = importlib.util.spec_from_file_location(
            name, root / "__init__.py", submodule_search_locations=[str(root)]
        )
        if spec is None or spec.loader is None:
            return unavailable("unexpected_input")
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
        module = importlib.import_module(name + ".verify")
        return module.main()
    except (Exception, KeyboardInterrupt):
        # No raw loader, path, API or transport exception reaches stderr.
        return unavailable("unexpected_input")


if __name__ == "__main__":
    raise SystemExit(main())
