"""Workspace isolation rules for parallel lanes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .models import BuildLane

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


class WorkspaceIsolationError(ValueError):
    """Raised when runtime or lane workspaces could overlap product files."""


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


@dataclass(frozen=True)
class WorkspacePolicy:
    product_root: Path
    workspace_root: Path
    runtime_root: Path

    def __post_init__(self) -> None:
        product = self.product_root.resolve()
        workspace = self.workspace_root.resolve()
        runtime = self.runtime_root.resolve()
        if _inside(workspace, product):
            raise WorkspaceIsolationError("workspace root must be outside the product checkout")
        if _inside(runtime, product):
            raise WorkspaceIsolationError("runtime root must be outside the product checkout")
        if workspace == runtime:
            raise WorkspaceIsolationError("workspace and runtime roots must be distinct")
        object.__setattr__(self, "product_root", product)
        object.__setattr__(self, "workspace_root", workspace)
        object.__setattr__(self, "runtime_root", runtime)

    def expected_path(self, lane: BuildLane) -> Path:
        if not _SAFE_COMPONENT.fullmatch(lane.lane_id):
            raise WorkspaceIsolationError("lane_id is not safe for a workspace path")
        return (self.workspace_root / lane.lane_id).resolve()

    def validate_backend_path(self, lane: BuildLane, path: str | Path) -> Path:
        actual = Path(path).resolve()
        expected = self.expected_path(lane)
        if actual != expected:
            raise WorkspaceIsolationError(
                f"backend workspace {actual} does not match expected lane path {expected}"
            )
        if _inside(actual, self.product_root):
            raise WorkspaceIsolationError("lane workspace must be outside the product checkout")
        return actual

    def assert_unique(self, assignments: list[Path] | tuple[Path, ...]) -> None:
        normalized = [path.resolve() for path in assignments]
        if len(normalized) != len(set(normalized)):
            raise WorkspaceIsolationError("parallel lanes must use unique workspaces")
