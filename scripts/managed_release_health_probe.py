"""Stable release health probe executed by the managed release's Python."""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

MANAGED_MODULES = (
    "msos_autobuilder.persistent_host",
    "msos_autobuilder.results_relay",
    "msos_autobuilder.candidate_gate_revisions",
    "msos_autobuilder.revision_loop",
    "msos_autobuilder.controlled_publisher",
    "msos_autobuilder.refill_controller",
)


def probe_release(
    release_root: str | Path,
    *,
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> dict[str, str]:
    root = Path(release_root).resolve()
    if not (root / "pyproject.toml").is_file():
        raise RuntimeError("managed release is missing pyproject.toml")
    source_root = root / "src"
    sys.path.insert(0, str(source_root))
    imported: dict[str, str] = {}
    try:
        for module_name in MANAGED_MODULES:
            module = importer(module_name)
            module_file = getattr(module, "__file__", None)
            if not module_file:
                raise RuntimeError(f"managed module has no file identity: {module_name}")
            origin = Path(module_file).resolve()
            try:
                origin.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(
                    f"managed module resolved outside the selected release: {module_name}={origin}"
                ) from exc
            imported[module_name] = str(origin)
    finally:
        if sys.path and sys.path[0] == str(source_root):
            sys.path.pop(0)
    return imported


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 1:
        raise SystemExit("usage: managed_release_health_probe.py <release-root>")
    imported = probe_release(args[0])
    print(json.dumps({"version": 1, "state": "healthy", "modules": imported}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
