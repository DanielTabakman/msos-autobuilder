"""Stable release health probe executed by the managed release's Python."""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

SERVICE_MODULES = {
    "host": ("msos_autobuilder.persistent_host",),
    "relay": ("msos_autobuilder.results_relay",),
    "gate": ("msos_autobuilder.candidate_gate_revisions",),
    "revision": ("msos_autobuilder.revision_loop",),
    "publisher": ("msos_autobuilder.controlled_publisher",),
    "refill": ("msos_autobuilder.refill_controller",),
}

MANAGED_MODULES = tuple(module for modules in SERVICE_MODULES.values() for module in modules)


def probe_release(
    release_root: str | Path,
    service_name: str | None = None,
    *,
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> dict[str, str]:
    root = Path(release_root).resolve()
    if not (root / "pyproject.toml").is_file():
        raise RuntimeError("managed release is missing pyproject.toml")
    source_root = root / "src"
    sys.path.insert(0, str(source_root))
    if service_name is None:
        services = ["host", "relay", "gate", "revision", "publisher"]
        if (root / "src" / "msos_autobuilder" / "refill_controller.py").is_file():
            services.append("refill")
        modules = tuple(module for service in services for module in SERVICE_MODULES[service])
    else:
        modules = SERVICE_MODULES.get(service_name)
        if modules is None:
            raise RuntimeError(f"unknown managed service for health probe: {service_name}")
    imported: dict[str, str] = {}
    try:
        for module_name in modules:
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
    if len(args) not in {1, 2}:
        raise SystemExit("usage: managed_release_health_probe.py <release-root> [service-name]")
    imported = probe_release(args[0], args[1] if len(args) == 2 else None)
    print(json.dumps({"version": 1, "state": "healthy", "modules": imported}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
